//go:build integration

package integration

import (
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestRecreateUpdatesAContainer(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", "--label", "keep=me", "-e", "EXTRA=1", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"})

	e.requireRunningImage("app", v2)
	if got := e.inspect("app", `{{index .Config.Labels "keep"}} {{range .Config.Env}}{{.}} {{end}}`); !strings.Contains(got, "me") || !strings.Contains(got, "EXTRA=1") {
		t.Errorf("the configuration of the container was not kept: %q", got)
	}
	e.waitFor("the new version to log", func() bool { return strings.Contains(docker(t, "logs", e.name("app")), "version 2") })
}

func TestReplaceUpdatesAContainerAndRemovesTheOldOne(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	oldID := e.run("app", "--network", "none", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	e.requireRunningImage("app", v2)
	if got := e.inspect("app", "{{.Id}}"); got == oldID {
		t.Error("the container was not replaced")
	}
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("the previous version was left behind: %v", left)
	}
	if _, err := tryDocker("inspect", oldID); err == nil {
		t.Error("the old container still exists")
	}
}

func TestReplaceKeepsTheRestartPolicy(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", "--restart", "unless-stopped", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	e.requireRunningImage("app", v2)
	if got := e.inspect("app", "{{.HostConfig.RestartPolicy.Name}}"); got != "unless-stopped" {
		t.Errorf("restart policy of the new container: %q", got)
	}
}

func TestReplaceRestoresTheOldContainerWhenTheNewOneCannotStart(t *testing.T) {
	e := newEnv(t)
	v1 := e.buildImage("v1", "ENV VERSION=1")
	e.buildImage("broken", `ENTRYPOINT ["/does-not-exist"]`)
	e.release("v1")
	oldID := e.run("app", "--network", "none", "--restart", "unless-stopped", e.image("latest"))
	e.release("broken")

	out := e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	e.requireRunningImage("app", v1)
	if got := e.inspect("app", "{{.Id}}"); got != oldID {
		t.Errorf("the container is not the original one: %s, want %s", got, oldID)
	}
	if got := e.inspect("app", "{{.HostConfig.RestartPolicy.Name}}"); got != "unless-stopped" {
		t.Errorf("the restart policy was not restored: %q", got)
	}
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("leftovers: %v", left)
	}
	if !strings.Contains(out, "previous version") {
		t.Errorf("the restore is not mentioned in the output")
	}
	// Nothing else may have been created under the name either
	if n := docker(t, "ps", "-a", "-q", "--filter", "name=^"+e.name("app")+"$"); len(strings.Fields(n)) != 1 {
		t.Errorf("expected exactly one container named %s, got %q", e.name("app"), n)
	}
}

func TestReplaceRestoresTheOldContainerWhenTheNewOneCrashes(t *testing.T) {
	e := newEnv(t)
	v1 := e.buildImage("v1", "ENV VERSION=1")
	e.buildImage("crashing", "ENV VERSION=2", "ENV MODE=slowcrash")
	e.release("v1")
	oldID := e.run("app", "--network", "none", e.image("latest"))
	e.release("crashing")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace", "--rollback-health-timeout", "6s")

	e.requireRunningImage("app", v1)
	if got := e.inspect("app", "{{.Id}}"); got != oldID {
		t.Errorf("the container is not the original one")
	}
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("leftovers: %v", left)
	}
}

func TestReplaceWithoutAHealthTimeoutDoesNotNoticeACrash(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	crashing := e.buildImage("crashing", "ENV VERSION=2", "ENV MODE=slowcrash")
	e.release("v1")
	e.run("app", "--network", "none", e.image("latest"))
	e.release("crashing")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	// Documented behaviour: starting is all that is required without --rollback-health-timeout
	if got := e.inspect("app", "{{.Image}}"); got != crashing {
		t.Errorf("expected the new image to be in place, got %s", got)
	}
}

func TestReplaceFallsBackToRecreateForAutoRemoveContainers(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", "--rm", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	e.requireRunningImage("app", v2)
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("leftovers: %v", left)
	}
}

func TestReplaceReusesAStaticIPAddress(t *testing.T) {
	e := newEnv(t)
	network := e.prefix + "-net"
	docker(t, "network", "create", "--subnet", "10.88.7.0/24", network)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", network, "--ip", "10.88.7.10", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	e.requireRunningImage("app", v2)
	if got := e.inspect("app", "{{(index .NetworkSettings.Networks \""+network+"\").IPAddress}}"); got != "10.88.7.10" {
		t.Errorf("the address of the new container: %q", got)
	}
}

func volumeCount(t *testing.T, e *env, container string) []string {
	t.Helper()
	return strings.Fields(docker(t, "inspect", "--format", `{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}`, container))
}

func volumeExists(name string) bool {
	_, err := tryDocker("volume", "inspect", name)
	return err == nil
}

func TestReplaceRemovesAnonymousVolumesOfTheOldContainerOnRequest(t *testing.T) {
	for _, strategy := range []string{"recreate", "replace"} {
		t.Run(strategy, func(t *testing.T) {
			e := newEnv(t)
			e.buildImage("v1", "ENV VERSION=1", "VOLUME /data")
			e.buildImage("v2", "ENV VERSION=2", "VOLUME /data")
			e.release("v1")
			e.run("app", "--network", "none", e.image("latest"))
			oldVolumes := volumeCount(t, e, e.name("app"))
			if len(oldVolumes) != 1 {
				t.Fatalf("expected one anonymous volume, got %v", oldVolumes)
			}
			e.release("v2")

			e.mustUpdate([]string{"app"}, "--update-strategy", strategy, "--remove-volumes")

			newVolumes := volumeCount(t, e, e.name("app"))
			if len(newVolumes) != 1 || newVolumes[0] == oldVolumes[0] {
				t.Fatalf("the new container has volumes %v, old was %v", newVolumes, oldVolumes)
			}
			if volumeExists(oldVolumes[0]) {
				t.Errorf("the old anonymous volume %s should have been removed", oldVolumes[0])
			}
			if !volumeExists(newVolumes[0]) {
				t.Errorf("the volume of the new container must still exist")
			}
			t.Cleanup(func() { _, _ = tryDocker("volume", "rm", "-f", oldVolumes[0], newVolumes[0]) })
		})
	}
}

func TestReplaceKeepsNamedVolumesAndTheirData(t *testing.T) {
	e := newEnv(t)
	volume := e.prefix + "-data"
	docker(t, "volume", "create", volume)
	t.Cleanup(func() { _, _ = tryDocker("volume", "rm", "-f", volume) })
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", "-v", volume+":/data", e.image("latest"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace", "--remove-volumes")

	e.requireRunningImage("app", v2)
	if !volumeExists(volume) {
		t.Fatal("a named volume must never be removed")
	}
	if got := e.inspect("app", `{{range .Mounts}}{{.Name}} {{end}}`); !strings.Contains(got, volume) {
		t.Errorf("the new container does not use the volume: %q", got)
	}
}

func TestRollbackOnFailureRecreatesFromThePreviousImage(t *testing.T) {
	e := newEnv(t)
	v1 := e.buildImage("v1", "ENV VERSION=1")
	e.buildImage("broken", `ENTRYPOINT ["/does-not-exist"]`)
	e.release("v1")
	e.run("app", "--network", "none", e.image("latest"))
	e.release("broken")

	e.mustUpdate([]string{"app"}, "--rollback-on-failure")

	e.requireRunningImage("app", v1)
}

func TestFailedStartDoesNotBlockTheNextUpdate(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.buildImage("broken", `ENTRYPOINT ["/does-not-exist"]`)
	e.release("v1")
	e.run("app", "--network", "none", e.image("latest"))
	e.release("broken")

	// Without a rollback the container is gone, but nothing must be left occupying its name
	e.mustUpdate([]string{"app"})
	if e.exists(e.name("app")) {
		t.Fatalf("an incomplete container was left behind")
	}
	e.release("v2")
	docker(t, "run", "-d", "--name", e.name("app"), "--network", "none", e.image("latest"))
	e.requireRunningImage("app", v2)
}

func TestStoppedContainersAreReplacedWithoutBeingStarted(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", e.image("latest"))
	docker(t, "stop", e.name("app"))
	e.release("v2")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace", "--include-stopped")

	if got := e.inspect("app", "{{.State.Running}}"); got != "false" {
		t.Errorf("a stopped container must stay stopped, running=%s", got)
	}
	if got := e.inspect("app", "{{.Image}}"); got != v2 {
		t.Errorf("image %s, want %s", got, v2)
	}
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("leftovers: %v", left)
	}
}

// leftBehind sets up what an interrupted replace update leaves: a stopped container with a backup name
func (e *env) leftBehind(original string, age time.Duration, policyAndState string) string {
	e.t.Helper()
	id := e.run("tmp", "--network", "none", e.image("latest"))
	docker(e.t, "stop", e.name("tmp"))
	backup := e.name(original) + "_wt-prev-" + strconv.FormatInt(time.Now().Add(-age).Unix(), 10) + "-" + policyAndState
	docker(e.t, "rename", e.name("tmp"), backup)
	return id
}

func TestInterruptedUpdateIsRecovered(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	e.release("v1")
	id := e.leftBehind("app", time.Hour, "ur") // was running, restart policy unless-stopped

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	if got := e.inspect("app", "{{.Id}}"); got != id {
		t.Fatalf("the backup was not restored: %s", got)
	}
	e.waitFor("the container to run again", func() bool { return e.inspect("app", "{{.State.Running}}") == "true" })
	if got := e.inspect("app", "{{.HostConfig.RestartPolicy.Name}}"); got != "unless-stopped" {
		t.Errorf("the restart policy was not restored: %q", got)
	}
}

func TestRecentBackupsAreLeftAlone(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	e.release("v1")
	e.leftBehind("app", time.Minute, "nr")

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	if e.exists(e.name("app")) {
		t.Error("a backup that may belong to an update in progress must not be touched")
	}
	if len(e.leftovers()) != 1 {
		t.Errorf("expected the backup to still be there, got %v", e.leftovers())
	}
}

func TestBackupIsRemovedWhenItsReplacementRuns(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	e.release("v1")
	e.leftBehind("app", time.Hour, "nr")
	e.run("app", "--network", "none", e.image("latest"))

	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")

	if len(e.leftovers()) != 0 {
		t.Errorf("the backup should have been removed: %v", e.leftovers())
	}
	if got := e.inspect("app", "{{.State.Running}}"); got != "true" {
		t.Errorf("the replacement must be left running")
	}
}

func TestRecoveryOnlyTouchesContainersInScope(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1")
	e.release("v1")
	e.leftBehind("app", time.Hour, "nr")

	// Another instance, watching other containers, must not restore it
	e.mustUpdate([]string{"unrelated"}, "--update-strategy", "replace")

	if e.exists(e.name("app")) {
		t.Fatal("a backup outside the containers this instance watches was restored")
	}
	if len(e.leftovers()) != 1 {
		t.Fatalf("expected the backup to be left alone, got %v", e.leftovers())
	}

	// The instance that does watch it does restore it
	e.mustUpdate([]string{"app"}, "--update-strategy", "replace")
	if !e.exists(e.name("app")) {
		t.Fatal("the backup was not restored by the instance watching it")
	}
}

func TestReplaceUpdatesAContainerThatIgnoresItsStopSignal(t *testing.T) {
	e := newEnv(t)
	e.buildImage("v1", "ENV VERSION=1", "ENV MODE=stubborn")
	v2 := e.buildImage("v2", "ENV VERSION=2")
	e.release("v1")
	e.run("app", "--network", "none", e.image("latest"))
	e.waitFor("the stubborn container to keep running", func() bool { return e.inspect("app", "{{.State.Running}}") == "true" })
	time.Sleep(time.Second)
	if e.inspect("app", "{{.State.Running}}") != "true" {
		t.Fatal("the test container exited by itself, the test would prove nothing")
	}
	e.release("v2")

	out := e.mustUpdate([]string{"app"}, "--update-strategy", "replace", "--stop-timeout", "2s")
	if !strings.Contains(out, "killing it") {
		t.Errorf("expected the container to be killed after ignoring SIGTERM")
	}

	e.requireRunningImage("app", v2)
	if left := e.leftovers(); len(left) != 0 {
		t.Errorf("leftovers: %v", left)
	}
}
