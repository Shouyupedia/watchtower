//go:build integration

// Package integration runs watchtower against a real Docker daemon.
//
// The tests need a daemon (DOCKER_HOST is honoured) and the docker CLI. No registry is needed: the images are
// imported from a tiny static program, and an update is simulated by pointing the tag at a different image and
// running watchtower with --no-pull.
//
//	go test -tags integration -count=1 ./test/integration/
package integration

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

var (
	appBinary  string // the program the test containers run
	watchtower string // the watchtower binary under test
)

const appSource = `package main

import (
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	fmt.Println("version", os.Getenv("VERSION"))
	switch os.Getenv("MODE") {
	case "crash":
		os.Exit(3)
	case "slowcrash":
		time.Sleep(2 * time.Second)
		os.Exit(4)
	case "stubborn":
		// ignores SIGTERM, so only SIGKILL stops it
		signal.Ignore(syscall.SIGTERM)
		for {
			time.Sleep(time.Hour)
		}
	}
	c := make(chan os.Signal, 1)
	signal.Notify(c, syscall.SIGTERM, syscall.SIGINT)
	<-c
}
`

func TestMain(m *testing.M) {
	os.Exit(run(m))
}

func run(m *testing.M) int {
	dir, err := os.MkdirTemp("", "wt-it-*")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer os.RemoveAll(dir)

	if _, err := exec.LookPath("docker"); err != nil {
		fmt.Fprintln(os.Stderr, "docker CLI not found")
		return 1
	}
	if out, err := exec.Command("docker", "version", "--format", "{{.Server.Version}}").CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "cannot reach a docker daemon: %v\n%s", err, out)
		return 1
	}

	src := filepath.Join(dir, "app")
	_ = os.MkdirAll(src, 0o755)
	_ = os.WriteFile(filepath.Join(src, "main.go"), []byte(appSource), 0o644)
	_ = os.WriteFile(filepath.Join(src, "go.mod"), []byte("module app\n\ngo 1.22\n"), 0o644)
	appBinary = filepath.Join(src, "app")
	build := exec.Command("go", "build", "-o", appBinary, ".")
	build.Dir = src
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		fmt.Fprintf(os.Stderr, "building the test app failed: %v\n%s", err, out)
		return 1
	}

	watchtower = os.Getenv("WATCHTOWER_BIN")
	if watchtower == "" {
		watchtower = filepath.Join(dir, "watchtower")
		root, _ := filepath.Abs("../..")
		wt := exec.Command("go", "build", "-o", watchtower, ".")
		wt.Dir = root
		if out, err := wt.CombinedOutput(); err != nil {
			fmt.Fprintf(os.Stderr, "building watchtower failed: %v\n%s", err, out)
			return 1
		}
	}

	return m.Run()
}

// docker runs the docker CLI and returns its trimmed output
func docker(t *testing.T, args ...string) string {
	t.Helper()
	out, err := tryDocker(args...)
	if err != nil {
		t.Fatalf("docker %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return out
}

func tryDocker(args ...string) (string, error) {
	out, err := exec.Command("docker", args...).CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

var unsafeName = regexp.MustCompile(`[^a-zA-Z0-9]+`)

// env gives every test its own names, and removes what it created when it is done
type env struct {
	t      *testing.T
	prefix string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	prefix := "wtit-" + strings.ToLower(unsafeName.ReplaceAllString(strings.TrimPrefix(t.Name(), "Test"), "-"))
	if len(prefix) > 40 {
		prefix = prefix[:40]
	}
	e := &env{t: t, prefix: prefix}
	e.cleanup()
	t.Cleanup(e.cleanup)
	return e
}

func (e *env) cleanup() {
	names, _ := tryDocker("ps", "-aq", "--filter", "name="+e.prefix)
	for _, id := range strings.Fields(names) {
		_, _ = tryDocker("rm", "-f", "-v", id)
	}
	images, _ := tryDocker("images", "-q", e.prefix+"/*")
	for _, id := range strings.Fields(images) {
		_, _ = tryDocker("rmi", "-f", id)
	}
	_, _ = tryDocker("network", "rm", e.prefix+"-net")
}

func (e *env) name(suffix string) string { return e.prefix + "-" + suffix }
func (e *env) image(tag string) string   { return e.prefix + "/app:" + tag }

// buildImage creates an image that runs the test app. The changes are Dockerfile instructions (ENV, ENTRYPOINT, ...).
func (e *env) buildImage(tag string, changes ...string) string {
	e.t.Helper()
	tar := filepath.Join(e.t.TempDir(), "app.tar")
	if out, err := exec.Command("tar", "-C", filepath.Dir(appBinary), "-cf", tar, "app").CombinedOutput(); err != nil {
		e.t.Fatalf("tar failed: %v\n%s", err, out)
	}
	args := []string{"import", "--change", `ENTRYPOINT ["/app"]`}
	for _, c := range changes {
		args = append(args, "--change", c)
	}
	args = append(args, tar, e.image(tag))
	docker(e.t, args...)
	return e.imageID(e.image(tag))
}

// release makes the tag "latest" point to the image, which is what pushing a new version to a registry amounts to
func (e *env) release(tag string) {
	e.t.Helper()
	docker(e.t, "tag", e.image(tag), e.image("latest"))
}

func (e *env) imageID(ref string) string {
	e.t.Helper()
	return docker(e.t, "image", "inspect", "--format", "{{.Id}}", ref)
}

func (e *env) run(suffix string, args ...string) string {
	e.t.Helper()
	full := append([]string{"run", "-d", "--name", e.name(suffix)}, args...)
	return docker(e.t, full...)
}

func (e *env) inspect(suffix, format string) string {
	e.t.Helper()
	return docker(e.t, "inspect", "--format", format, e.name(suffix))
}

func (e *env) exists(name string) bool {
	_, err := tryDocker("inspect", name)
	return err == nil
}

// leftovers lists the containers set aside by the replace strategy
func (e *env) leftovers() []string {
	out, _ := tryDocker("ps", "-a", "--format", "{{.Names}}", "--filter", "name=_wt-prev-")
	var names []string
	for _, n := range strings.Fields(out) {
		if strings.HasPrefix(n, e.prefix) {
			names = append(names, n)
		}
	}
	return names
}

// waitFor polls until the condition holds
func (e *env) waitFor(what string, cond func() bool) {
	e.t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	e.t.Fatalf("timed out waiting for %s", what)
}

// update runs watchtower once, against the given containers
func (e *env) update(containers []string, args ...string) (string, error) {
	e.t.Helper()
	full := []string{"--run-once", "--no-pull", "--no-startup-message", "--stop-timeout", "10s"}
	full = append(full, args...)
	for _, c := range containers {
		full = append(full, e.name(c))
	}
	cmd := exec.Command(watchtower, full...)
	out, err := cmd.CombinedOutput()
	e.t.Logf("watchtower %s\n%s", strings.Join(full, " "), out)
	return string(out), err
}

func (e *env) mustUpdate(containers []string, args ...string) string {
	e.t.Helper()
	out, err := e.update(containers, args...)
	if err != nil {
		e.t.Fatalf("watchtower failed: %v", err)
	}
	return out
}

func (e *env) requireRunningImage(suffix, imageID string) {
	e.t.Helper()
	if got := e.inspect(suffix, "{{.State.Running}}"); got != "true" {
		e.t.Fatalf("%s is not running (running=%s)", e.name(suffix), got)
	}
	if got := e.inspect(suffix, "{{.Image}}"); got != imageID {
		e.t.Fatalf("%s runs image %s, want %s", e.name(suffix), got, imageID)
	}
}
