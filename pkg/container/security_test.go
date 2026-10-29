package container

import (
	"context"
	"net"
	"testing"
	"time"

	wt "github.com/containrrr/watchtower/pkg/types"
	sdkClient "github.com/moby/moby/client"
)

func TestCreateConfigPreservesRunningUser(t *testing.T) {
	for _, user := range []string{"1000:1000", "app", "root", ""} {
		c := MockContainer()
		c.containerInfo.Config.User = user
		c.imageInfo.Config.User = user
		if got := c.GetCreateConfig().User; got != user {
			t.Fatalf("running user %q changed to %q", user, got)
		}
	}
}

type stalledExecClient struct {
	sdkClient.APIClient
	deadline time.Time
	attached net.Conn
	peer     net.Conn
}

func (c *stalledExecClient) ExecCreate(ctx context.Context, _ string, _ sdkClient.ExecCreateOptions) (sdkClient.ExecCreateResult, error) {
	c.deadline, _ = ctx.Deadline()
	return sdkClient.ExecCreateResult{ID: "dummy-exec"}, nil
}

func (c *stalledExecClient) ExecAttach(ctx context.Context, _ string, _ sdkClient.ExecAttachOptions) (sdkClient.ExecAttachResult, error) {
	c.deadline, _ = ctx.Deadline()
	c.attached, c.peer = net.Pipe()
	return sdkClient.ExecAttachResult{HijackedResponse: sdkClient.NewHijackedResponse(c.attached, "")}, nil
}

func (c *stalledExecClient) ExecStart(context.Context, string, sdkClient.ExecStartOptions) (sdkClient.ExecStartResult, error) {
	return sdkClient.ExecStartResult{}, nil
}

func (c *stalledExecClient) ExecInspect(ctx context.Context, _ string, _ sdkClient.ExecInspectOptions) (sdkClient.ExecInspectResult, error) {
	return sdkClient.ExecInspectResult{Running: true}, ctx.Err()
}

func TestExecDeadlineCoversAttachedOutput(t *testing.T) {
	api := &stalledExecClient{}
	client := dockerClient{api: api}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := client.executeCommand(ctx, wt.ContainerID("dummy"), "sleep forever")
		done <- err
	}()
	select {
	case err := <-done:
		if err != context.DeadlineExceeded {
			t.Fatalf("unexpected hook error: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("hook output read ignored its deadline")
	}
	if api.peer != nil {
		defer api.peer.Close()
	}
	if api.deadline.IsZero() {
		t.Fatal("hook deadline did not reach the Docker API")
	}
}
