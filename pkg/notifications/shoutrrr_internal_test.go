package notifications

import (
	"sync"
	"testing"
	"time"

	"github.com/containrrr/shoutrrr/pkg/types"
	"github.com/sirupsen/logrus"
)

type recordingRouter struct {
	mu   sync.Mutex
	sent []string
}

func (r *recordingRouter) Send(message string, _ *types.Params) []error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sent = append(r.sent, message)
	return nil
}

func newTestNotifier(t *testing.T, tpl string) (*shoutrrrTypeNotifier, *recordingRouter) {
	t.Helper()
	router := &recordingRouter{}
	n := &shoutrrrTypeNotifier{
		Urls:     []string{"logger://"},
		Router:   router,
		messages: make(chan string, 1),
		done:     make(chan bool),
		logLevel: logrus.InfoLevel,
		params:   &types.Params{},
	}
	tplParsed, err := getShoutrrrTemplate(tpl, true)
	if err != nil {
		t.Fatal(err)
	}
	n.template = tplParsed
	go sendNotifications(n)
	return n, router
}

func TestNotifierIsSafeForConcurrentLogging(t *testing.T) {
	n, _ := newTestNotifier(t, "")

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				_ = n.Fire(&logrus.Entry{Level: logrus.InfoLevel, Message: "msg"})
			}
		}()
	}
	for i := 0; i < 20; i++ {
		n.StartNotification()
		n.SendNotification(nil)
	}
	wg.Wait()
	n.Close()
}

func TestFireAfterCloseDoesNotPanic(t *testing.T) {
	n, _ := newTestNotifier(t, "")
	n.Close()

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("logging after Close panicked: %v", r)
		}
	}()
	_ = n.Fire(&logrus.Entry{Level: logrus.InfoLevel, Message: "late"})
	n.Close() // closing twice must be harmless too
}

func TestTemplateErrorIsNotFatal(t *testing.T) {
	// Executing .Missing on a legacy ([]*Entry) template data fails at runtime
	n, _ := newTestNotifier(t, "{{ .Missing.Field }}")

	exited := make(chan struct{})
	oldExit := logrus.StandardLogger().ExitFunc
	logrus.StandardLogger().ExitFunc = func(int) { close(exited) }
	defer func() { logrus.StandardLogger().ExitFunc = oldExit }()

	n.sendEntries([]*logrus.Entry{{Level: logrus.InfoLevel, Message: "x"}}, nil)

	select {
	case <-exited:
		t.Fatal("a broken notification template must not terminate the process")
	case <-time.After(200 * time.Millisecond):
	}
	n.Close()
}
