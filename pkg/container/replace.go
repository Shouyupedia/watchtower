package container

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	log "github.com/shouyupedia/watchtower/internal/logging"

	t "github.com/shouyupedia/watchtower/pkg/types"
)

// ErrNotPreservable is returned for containers that cannot be kept around under another name while their
// replacement is started, for example because the daemon removes them as soon as they stop
var ErrNotPreservable = errors.New("the container cannot be preserved")

const (
	backupMarker = "_wt-prev-"

	// minBackupAge is how old a backup has to be before it is considered left behind by a watchtower that
	// was interrupted, rather than belonging to an update that is in progress (possibly in another instance)
	minBackupAge = 10 * time.Minute

	healthPollInterval = time.Second
)

// A backup is named <original name>_wt-prev-<unix time>-<policy><state>. The restart policy and whether
// the container was running are part of the name, as that is the only place to keep them that survives
// watchtower being interrupted: neither labels nor anything else can be added to an existing container.
var backupNamePattern = regexp.MustCompile(`^(.+)` + backupMarker + `(\d+)-([nauf])(\d*)([rs])$`)

func restartPolicyCode(p container.RestartPolicy) string {
	switch p.Name {
	case container.RestartPolicyAlways:
		return "a"
	case container.RestartPolicyUnlessStopped:
		return "u"
	case container.RestartPolicyOnFailure:
		return "f" + strconv.Itoa(p.MaximumRetryCount)
	}
	return "n"
}

func restartPolicyFromCode(code, retries string) container.RestartPolicy {
	switch code {
	case "a":
		return container.RestartPolicy{Name: container.RestartPolicyAlways}
	case "u":
		return container.RestartPolicy{Name: container.RestartPolicyUnlessStopped}
	case "f":
		n, _ := strconv.Atoi(retries)
		return container.RestartPolicy{Name: container.RestartPolicyOnFailure, MaximumRetryCount: n}
	}
	return container.RestartPolicy{Name: container.RestartPolicyDisabled}
}

func restartsAutomatically(p container.RestartPolicy) bool {
	return p.Name != "" && p.Name != container.RestartPolicyDisabled
}

func backupName(name string, now time.Time, policy container.RestartPolicy, running bool) string {
	state := "s"
	if running {
		state = "r"
	}
	return fmt.Sprintf("%s%s%d-%s%s", strings.TrimPrefix(name, "/"), backupMarker, now.Unix(), restartPolicyCode(policy), state)
}

func (client dockerClient) setRestartPolicy(ctx context.Context, id string, policy container.RestartPolicy) error {
	ctx, cancel := callCtx(ctx)
	defer cancel()
	_, err := client.api.ContainerUpdate(ctx, id, container.UpdateConfig{RestartPolicy: policy})
	return err
}

// StopAndPreserve stops the container and renames it so that its name is free for the replacement, keeping
// everything else about it so that RestoreContainer can bring it back
func (client dockerClient) StopAndPreserve(ctx context.Context, c t.Container, timeout time.Duration) (t.ContainerBackup, error) {
	info := c.ContainerInfo()
	if info.HostConfig.AutoRemove {
		return t.ContainerBackup{}, ErrNotPreservable
	}

	policy := info.HostConfig.RestartPolicy
	backup := t.ContainerBackup{
		ID:            c.ID(),
		OriginalName:  c.Name(),
		BackupName:    backupName(c.Name(), time.Now(), policy, c.IsRunning()),
		WasRunning:    c.IsRunning(),
		RestartPolicy: policy,
	}
	idStr := string(c.ID())
	shortID := c.ID().ShortID()

	// The daemon would otherwise bring the container back to life behind our back, both right after it is
	// killed and when it is restarted itself, at which point it could take the ports of the replacement.
	changedPolicy := restartsAutomatically(policy)
	if changedPolicy {
		if err := client.setRestartPolicy(ctx, idStr, container.RestartPolicy{Name: container.RestartPolicyDisabled}); err != nil {
			return t.ContainerBackup{}, fmt.Errorf("could not disable the restart policy of %s: %w", c.Name(), err)
		}
	}

	// undo puts the container back the way it was after something went wrong
	undo := func() {
		undoCtx := context.WithoutCancel(ctx)
		if changedPolicy {
			if err := client.setRestartPolicy(undoCtx, idStr, policy); err != nil {
				log.WithError(err).Warnf("Could not restore the restart policy of %s", c.Name())
			}
		}
		if backup.WasRunning {
			startCtx, cancel := callCtx(undoCtx)
			defer cancel()
			if err := client.api.ContainerStart(startCtx, idStr, container.StartOptions{}); err != nil {
				log.WithError(err).Warnf("Could not start %s again", c.Name())
			}
		}
	}

	if c.IsRunning() {
		signal := c.StopSignal()
		if signal == "" {
			signal = defaultStopSignal
		}
		log.Infof("Stopping %s (%s) with %s", c.Name(), shortID, signal)
		killCtx, cancel := callCtx(ctx)
		err := client.api.ContainerKill(killCtx, idStr, signal)
		cancel()
		if err != nil {
			if changedPolicy {
				_ = client.setRestartPolicy(context.WithoutCancel(ctx), idStr, policy)
			}
			return t.ContainerBackup{}, err
		}

		_ = client.waitForStopOrTimeout(ctx, c, timeout)
		inspectCtx, cancel := callCtx(ctx)
		state, err := client.api.ContainerInspect(inspectCtx, idStr)
		cancel()
		if err != nil {
			return t.ContainerBackup{}, err
		}
		if state.State != nil && state.State.Running {
			// Nothing but the restart policy has been touched, and the container is still doing its job
			if changedPolicy {
				_ = client.setRestartPolicy(context.WithoutCancel(ctx), idStr, policy)
			}
			return t.ContainerBackup{}, fmt.Errorf("container %s (%s) did not stop within %v", c.Name(), shortID, timeout)
		}
	}

	renameCtx, cancel := callCtx(ctx)
	defer cancel()
	log.Debugf("Renaming container %s (%s) to %s", c.Name(), shortID, backup.BackupName)
	if err := client.api.ContainerRename(renameCtx, idStr, backup.BackupName); err != nil {
		undo()
		return t.ContainerBackup{}, err
	}

	return backup, nil
}

// RestoreContainer puts a container preserved by StopAndPreserve back under its own name, and starts it if it was running
func (client dockerClient) RestoreContainer(ctx context.Context, b t.ContainerBackup) error {
	ctx = context.WithoutCancel(ctx)
	id := string(b.ID)

	renameCtx, cancel := callCtx(ctx)
	err := client.api.ContainerRename(renameCtx, id, strings.TrimPrefix(b.OriginalName, "/"))
	cancel()
	if err != nil {
		return fmt.Errorf("could not rename %s back to %s: %w", b.BackupName, b.OriginalName, err)
	}

	if restartsAutomatically(b.RestartPolicy) {
		if err := client.setRestartPolicy(ctx, id, b.RestartPolicy); err != nil {
			log.WithError(err).Warnf("Could not restore the restart policy of %s", b.OriginalName)
		}
	}

	if b.WasRunning {
		startCtx, cancel := callCtx(ctx)
		defer cancel()
		if err := client.api.ContainerStart(startCtx, id, container.StartOptions{}); err != nil {
			return fmt.Errorf("could not start %s again: %w", b.OriginalName, err)
		}
	}
	return nil
}

// RemoveBackup removes a container preserved by StopAndPreserve, once its replacement has proven itself
func (client dockerClient) RemoveBackup(ctx context.Context, b t.ContainerBackup) error {
	ctx, cancel := callCtx(ctx)
	defer cancel()
	log.Debugf("Removing the previous version of %s (%s)", b.OriginalName, b.ID.ShortID())
	err := client.api.ContainerRemove(ctx, string(b.ID), container.RemoveOptions{Force: true, RemoveVolumes: client.RemoveVolumes})
	if err != nil && cerrdefs.IsNotFound(err) {
		return nil
	}
	return err
}

// DiscardContainer force removes a container, for example a replacement that did not work out
func (client dockerClient) DiscardContainer(ctx context.Context, id t.ContainerID) error {
	ctx, cancel := callCtx(context.WithoutCancel(ctx))
	defer cancel()
	err := client.api.ContainerRemove(ctx, string(id), container.RemoveOptions{Force: true})
	if err != nil && cerrdefs.IsNotFound(err) {
		return nil
	}
	return err
}

// WaitUntilHealthy returns once the container has shown it works, or an error if it did not within the timeout.
// A container with a health check has to become healthy. One without has to still be running, without having
// been restarted, once the timeout has passed. A timeout of zero or less skips the wait.
func (client dockerClient) WaitUntilHealthy(ctx context.Context, id t.ContainerID, timeout time.Duration) error {
	if timeout <= 0 {
		return nil
	}

	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	for {
		inspectCtx, cancel := callCtx(ctx)
		info, err := client.api.ContainerInspect(inspectCtx, string(id))
		cancel()
		if err != nil {
			return err
		}

		state := info.State
		switch {
		case state == nil:
			return errors.New("the daemon did not report the state of the container")
		case !state.Running && !state.Restarting:
			return fmt.Errorf("the container exited with code %d", state.ExitCode)
		case state.Restarting || info.RestartCount > 0:
			return errors.New("the container keeps restarting")
		case state.Health != nil && state.Health.Status == "unhealthy":
			return errors.New("the container reported itself unhealthy")
		case state.Health != nil && state.Health.Status == "healthy":
			return nil
		}

		select {
		case <-deadline.C:
			if state.Health != nil {
				return fmt.Errorf("the container was still %q after %v", state.Health.Status, timeout)
			}
			return nil
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(healthPollInterval):
		}
	}
}

// RecoverBackups deals with containers that were set aside by an update that never finished, for example
// because watchtower was killed halfway: the previous version is put back if nothing took its place, and
// removed if its replacement is running. Anything unclear is left alone and reported.
func (client dockerClient) RecoverBackups(ctx context.Context) error {
	listCtx, cancel := callCtx(ctx)
	defer cancel()
	list, err := client.api.ContainerList(listCtx, container.ListOptions{
		All:     true,
		Filters: filters.NewArgs(filters.Arg("name", backupMarker)),
	})
	if err != nil {
		return err
	}

	for _, summary := range list {
		if len(summary.Names) == 0 {
			continue
		}
		name := strings.TrimPrefix(summary.Names[0], "/")
		match := backupNamePattern.FindStringSubmatch(name)
		if match == nil {
			continue
		}

		created, _ := strconv.ParseInt(match[2], 10, 64)
		if time.Since(time.Unix(created, 0)) < minBackupAge {
			continue
		}

		backup := t.ContainerBackup{
			ID:            t.ContainerID(summary.ID),
			OriginalName:  "/" + match[1],
			BackupName:    name,
			WasRunning:    match[5] == "r",
			RestartPolicy: restartPolicyFromCode(match[3], match[4]),
		}
		client.recoverBackup(ctx, backup)
	}
	return nil
}

func (client dockerClient) recoverBackup(ctx context.Context, backup t.ContainerBackup) {
	clog := log.WithFields(log.Fields{"container": backup.OriginalName, "backup": backup.BackupName})

	inspectCtx, cancel := callCtx(ctx)
	current, err := client.api.ContainerInspect(inspectCtx, strings.TrimPrefix(backup.OriginalName, "/"))
	cancel()

	switch {
	case err != nil && cerrdefs.IsNotFound(err):
		clog.Warn("An earlier update did not finish. Restoring the previous version of the container.")
		if err := client.RestoreContainer(ctx, backup); err != nil {
			clog.WithError(err).Error("Could not restore the previous version of the container")
		}
	case err != nil:
		clog.WithError(err).Warn("Could not check whether a left over backup of a container is still needed")
	case current.State != nil && current.State.Running:
		clog.Info("Removing the left over previous version of a container, its replacement is running.")
		if err := client.RemoveBackup(ctx, backup); err != nil {
			clog.WithError(err).Warn("Could not remove the left over previous version of the container")
		}
	default:
		clog.Warn("A previous version of the container was left behind by an update, but the container that replaced it is not running. Both have been left alone: check them and remove or rename the backup.")
	}
}
