package notifications

import (
	"net/url"
	"strings"
	"testing"
)

func TestLegacyEmailRejectsTLSDowngrade(t *testing.T) {
	notifier := &emailTypeNotifier{tlsSkipVerify: true}
	if result, err := notifier.GetURL(nil); err == nil || result != "" {
		t.Fatal("legacy TLS exception must not generate an unencrypted SMTP URL")
	}
}

func TestLegacyEmailKeepsEncryptionDefaults(t *testing.T) {
	notifier := &emailTypeNotifier{
		From: "sender@example.com", To: "recipient@example.com",
		Server: "smtp.example.com", Port: 587,
	}
	result, err := notifier.GetURL(nil)
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(result)
	if err != nil {
		t.Fatal(err)
	}
	if strings.EqualFold(u.Query().Get("encryption"), "none") || strings.EqualFold(u.Query().Get("usestarttls"), "false") {
		t.Fatalf("SMTP encryption disabled: %s", result)
	}
}
