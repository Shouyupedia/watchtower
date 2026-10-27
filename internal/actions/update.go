package actions

import (
	"context"
	"errors"
	"sync"

	"github.com/shouyupedia/watchtower/internal/util"
	"github.com/shouyupedia/watchtower/pkg/container"
	"github.com/shouyupedia/watchtower/pkg/lifecycle"
	"github.com/shouyupedia/watchtower/pkg/session"
	"github.com/shouyupedia/watchtower/pkg/sorter"
	"github.com/shouyupedia/watchtower/pkg/types"
	log "github.com/sirupsen/logrus"
)

// Update looks at the running Docker containers to see if any of the images
// used to start those containers have been updated. If a change is detected in
// any of the images, the associated containers are stopped and restarted with
// the new image.
func Update(ctx context.Context, client container.Client, params types.UpdateParams) (types.Report, error) {
	log.Debug("Checking containers for updated images")
	progress := &session.Progress{}
	staleCount := 0

	containers, err := client.ListContainers(ctx, params.Filter)
	if err != nil {
		return nil, err
	}

	if params.LifecycleHooks {
		// The containers have just been listed, so there is no need to ask the daemon for them again
		lifecycle.ExecutePreChecks(ctx, client, containers)
	}

	staleCheckFailed := 0

	// Checking for new images means talking to registries, so let several checks wait for them at the same time.
	// The results are applied below in the original order.
	checks := checkStaleness(ctx, client, containers, params)

	for i, targetContainer := range containers {
		stale, newestImage, err := checks[i].stale, checks[i].newestImage, checks[i].err
		shouldUpdate := stale && !params.NoRestart && !targetContainer.IsMonitorOnly(params)
		if err == nil && shouldUpdate {
			// Check to make sure we have all the necessary information for recreating the container
			err = targetContainer.VerifyConfiguration()
			// If the image information is incomplete and trace logging is enabled, log it for further diagnosis
			if err != nil && log.IsLevelEnabled(log.TraceLevel) {
				imageInfo := targetContainer.ImageInfo()
				log.Tracef("Image info: %#v", imageInfo)
				log.Tracef("Container info: %#v", targetContainer.ContainerInfo())
				if imageInfo != nil {
					log.Tracef("Image config: %#v", imageInfo.Config)
				}
			}
		}

		if err != nil {
			log.Infof("Unable to update container %q: %v. Proceeding to next.", targetContainer.Name(), err)
			stale = false
			staleCheckFailed++
			progress.AddSkipped(targetContainer, err)
		} else {
			progress.AddScanned(targetContainer, newestImage)
		}
		containers[i].SetStale(stale)

		if stale {
			staleCount++
		}
	}

	containers, err = sorter.SortByDependencies(containers)
	if err != nil {
		return nil, err
	}

	UpdateImplicitRestart(containers)

	var containersToUpdate []types.Container
	for _, c := range containers {
		if !c.IsMonitorOnly(params) {
			containersToUpdate = append(containersToUpdate, c)
			progress.MarkForUpdate(c.ID())
		}
	}

	if params.RollingRestart {
		progress.UpdateFailed(performRollingRestart(ctx, containersToUpdate, client, params))
	} else {
		failedStop, stoppedImages := stopContainersInReversedOrder(ctx, containersToUpdate, client, params)
		progress.UpdateFailed(failedStop)
		failedStart := restartContainersInSortedOrder(ctx, containersToUpdate, client, params, stoppedImages)
		progress.UpdateFailed(failedStart)
	}

	if params.LifecycleHooks {
		lifecycle.ExecutePostChecks(ctx, client, params)
	}
	return progress.Report(), nil
}

type staleCheck struct {
	stale       bool
	newestImage types.ImageID
	err         error
}

// checkStaleness runs IsContainerStale for every container, up to params.CheckConcurrency at a time
func checkStaleness(ctx context.Context, client container.Client, containers []types.Container, params types.UpdateParams) []staleCheck {
	results := make([]staleCheck, len(containers))
	check := func(i int) {
		results[i].stale, results[i].newestImage, results[i].err = client.IsContainerStale(ctx, containers[i], params)
	}

	if params.CheckConcurrency <= 1 || len(containers) <= 1 {
		for i := range containers {
			check(i)
		}
		return results
	}

	var wg sync.WaitGroup
	slots := make(chan struct{}, params.CheckConcurrency)
	for i := range containers {
		slots <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-slots }()
			check(i)
		}()
	}
	wg.Wait()
	return results
}

func performRollingRestart(ctx context.Context, containers []types.Container, client container.Client, params types.UpdateParams) map[types.ContainerID]error {
	cleanupImageIDs := make(map[types.ImageID]bool, len(containers))
	failed := make(map[types.ContainerID]error, len(containers))

	for i := len(containers) - 1; i >= 0; i-- {
		if containers[i].ToRestart() {
			err := stopStaleContainer(ctx, containers[i], client, params)
			if err != nil {
				failed[containers[i].ID()] = err
			} else {
				if err := restartStaleContainer(ctx, containers[i], client, params); err != nil {
					failed[containers[i].ID()] = err
				} else if containers[i].IsStale() {
					// Only add (previously) stale containers' images to cleanup
					cleanupImageIDs[containers[i].ImageID()] = true
				}
			}
		}
	}

	if params.Cleanup {
		cleanupImages(ctx, client, cleanupImageIDs)
	}
	return failed
}

func stopContainersInReversedOrder(ctx context.Context, containers []types.Container, client container.Client, params types.UpdateParams) (failed map[types.ContainerID]error, stopped map[types.ImageID]bool) {
	failed = make(map[types.ContainerID]error, len(containers))
	stopped = make(map[types.ImageID]bool, len(containers))
	for i := len(containers) - 1; i >= 0; i-- {
		if err := stopStaleContainer(ctx, containers[i], client, params); err != nil {
			failed[containers[i].ID()] = err
		} else {
			// NOTE: If a container is restarted due to a dependency this might be empty
			stopped[containers[i].SafeImageID()] = true
		}

	}
	return
}

func stopStaleContainer(ctx context.Context, container types.Container, client container.Client, params types.UpdateParams) error {
	if container.IsWatchtower() {
		log.Debugf("This is the watchtower container %s", container.Name())
		return nil
	}

	if !container.ToRestart() {
		return nil
	}

	// Perform an additional check here to prevent us from stopping a linked container we cannot restart
	if container.IsLinkedToRestarting() {
		if err := container.VerifyConfiguration(); err != nil {
			return err
		}
	}

	if params.LifecycleHooks {
		skipUpdate, err := lifecycle.ExecutePreUpdateCommand(ctx, client, container)
		if err != nil {
			log.Error(err)
			log.Info("Skipping container as the pre-update command failed")
			return err
		}
		if skipUpdate {
			log.Debug("Skipping container as the pre-update command returned exit code 75 (EX_TEMPFAIL)")
			return errors.New("skipping container as the pre-update command returned exit code 75 (EX_TEMPFAIL)")
		}
	}

	if err := client.StopContainer(ctx, container, params.Timeout); err != nil {
		log.Error(err)
		return err
	}
	return nil
}

func restartContainersInSortedOrder(ctx context.Context, containers []types.Container, client container.Client, params types.UpdateParams, stoppedImages map[types.ImageID]bool) map[types.ContainerID]error {
	cleanupImageIDs := make(map[types.ImageID]bool, len(containers))
	failed := make(map[types.ContainerID]error, len(containers))

	for _, c := range containers {
		if !c.ToRestart() {
			continue
		}
		if stoppedImages[c.SafeImageID()] {
			if err := restartStaleContainer(ctx, c, client, params); err != nil {
				failed[c.ID()] = err
			} else if c.IsStale() {
				// Only add (previously) stale containers' images to cleanup
				cleanupImageIDs[c.ImageID()] = true
			}
		}
	}

	if params.Cleanup {
		cleanupImages(ctx, client, cleanupImageIDs)
	}

	return failed
}

func cleanupImages(ctx context.Context, client container.Client, imageIDs map[types.ImageID]bool) {
	for imageID := range imageIDs {
		if imageID == "" {
			continue
		}
		if err := client.RemoveImageByID(ctx, imageID); err != nil {
			log.Error(err)
		}
	}
}

func restartStaleContainer(ctx context.Context, container types.Container, client container.Client, params types.UpdateParams) error {
	// Since we can't shutdown a watchtower container immediately, we need to
	// start the new one while the old one is still running. This prevents us
	// from re-using the same container name so we first rename the current
	// instance so that the new one can adopt the old name.
	if container.IsWatchtower() {
		if err := client.RenameContainer(ctx, container, util.RandName()); err != nil {
			log.Error(err)
			return nil
		}
	}

	if !params.NoRestart {
		if newContainerID, err := client.StartContainer(ctx, container); err != nil {
			log.Error(err)
			return err
		} else if container.ToRestart() && params.LifecycleHooks {
			lifecycle.ExecutePostUpdateCommand(ctx, client, newContainerID)
		}
	}
	return nil
}

// UpdateImplicitRestart iterates through the passed containers, setting the
// `LinkedToRestarting` flag if any of it's linked containers are marked for restart
func UpdateImplicitRestart(containers []types.Container) {

	for ci, c := range containers {
		if c.ToRestart() {
			// The container is already marked for restart, no need to check
			continue
		}

		if link := linkedContainerMarkedForRestart(c.Links(), containers); link != "" {
			log.WithFields(log.Fields{
				"restarting": link,
				"linked":     c.Name(),
			}).Debug("container is linked to restarting")
			// NOTE: To mutate the array, the `c` variable cannot be used as it's a copy
			containers[ci].SetLinkedToRestarting(true)
		}

	}
}

// linkedContainerMarkedForRestart returns the name of the first link that matches a
// container marked for restart
func linkedContainerMarkedForRestart(links []string, containers []types.Container) string {
	for _, linkName := range links {
		for _, candidate := range containers {
			if candidate.Name() == linkName && candidate.ToRestart() {
				return linkName
			}
		}
	}
	return ""
}
