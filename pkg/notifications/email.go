package notifications

import (
	"os"
	"time"

	"github.com/spf13/cobra"

	shoutrrrSmtp "github.com/containrrr/shoutrrr/pkg/services/smtp"
	t "github.com/shouyupedia/watchtower/pkg/types"
	log "github.com/sirupsen/logrus"
)

const (
	emailType = "email"
)

type emailTypeNotifier struct {
	From, To               string
	Server, User, Password string
	Port                   int
	entries                []*log.Entry
	delay                  time.Duration
}

func newEmailNotifier(c *cobra.Command) t.ConvertibleNotifier {
	flags := c.Flags()

	from, _ := flags.GetString("notification-email-from")
	to, _ := flags.GetString("notification-email-to")
	server, _ := flags.GetString("notification-email-server")
	user, _ := flags.GetString("notification-email-server-user")
	password, _ := flags.GetString("notification-email-server-password")
	port, _ := flags.GetInt("notification-email-server-port")
	if port < 1 || port > 65535 {
		log.Fatalf("Invalid SMTP server port %d", port)
	}
	if _, set := os.LookupEnv("WATCHTOWER_NOTIFICATION_EMAIL_SERVER_TLS_SKIP_VERIFY"); set {
		log.Fatal("WATCHTOWER_NOTIFICATION_EMAIL_SERVER_TLS_SKIP_VERIFY is no longer supported: the SMTP server must present a trusted TLS certificate")
	}
	delay, _ := flags.GetInt("notification-email-delay")

	n := &emailTypeNotifier{
		entries:  []*log.Entry{},
		From:     from,
		To:       to,
		Server:   server,
		User:     user,
		Password: password,
		Port:     port,
		delay:    time.Duration(delay) * time.Second,
	}

	return n
}

func (e *emailTypeNotifier) GetURL(c *cobra.Command) (string, error) {
	conf := &shoutrrrSmtp.Config{
		FromAddress: e.From,
		FromName:    "Watchtower",
		ToAddresses: []string{e.To},
		Port:        uint16(e.Port), //nolint:gosec // range checked in newEmailNotifier
		Host:        e.Server,
		Username:    e.User,
		Password:    e.Password,
		UseStartTLS: true,
		UseHTML:     false,
		Encryption:  shoutrrrSmtp.EncMethods.Auto,
		Auth:        shoutrrrSmtp.AuthTypes.None,
		ClientHost:  "localhost",
	}

	if len(e.User) > 0 {
		conf.Auth = shoutrrrSmtp.AuthTypes.Plain
	}

	return conf.GetURL().String(), nil
}

func (e *emailTypeNotifier) GetDelay() time.Duration {
	return e.delay
}
