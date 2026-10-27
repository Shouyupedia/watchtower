package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
)

const (
	token = "123123123"
)

func TestAPI(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "API Suite")
}

var _ = Describe("API", func() {
	api := New(token)

	Describe("RequireToken middleware", func() {
		It("should return 401 Unauthorized when token is not provided", func() {
			handlerFunc := api.RequireToken(testHandler)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/hello", nil)

			handlerFunc(rec, req)

			Expect(rec.Code).To(Equal(http.StatusUnauthorized))
		})

		It("should return 401 Unauthorized when token is invalid", func() {
			handlerFunc := api.RequireToken(testHandler)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/hello", nil)
			req.Header.Set("Authorization", "Bearer 123")

			handlerFunc(rec, req)

			Expect(rec.Code).To(Equal(http.StatusUnauthorized))
		})

		It("should return 200 OK when token is valid", func() {
			handlerFunc := api.RequireToken(testHandler)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/hello", nil)
			req.Header.Set("Authorization", "Bearer "+token)

			handlerFunc(rec, req)

			Expect(rec.Code).To(Equal(http.StatusOK))
		})
	})

	Describe("Start", func() {
		It("should do nothing when no handlers are registered", func() {
			Expect(New("").Start(false)).To(Succeed())
		})
		It("should refuse to start without a token", func() {
			a := New("")
			a.RegisterFunc("/hello", testHandler)
			Expect(a.Start(false)).To(MatchError(ContainSubstring("api token")))
		})
		It("should refuse a TLS certificate without a key", func() {
			a := New(token)
			a.RegisterFunc("/hello", testHandler)
			a.TLSCertFile = "cert.pem"
			Expect(a.Start(false)).To(HaveOccurred())
		})
		It("should return an error instead of exiting when the address cannot be bound", func() {
			a := New(token)
			a.RegisterFunc("/hello", testHandler)
			a.Addr = "256.256.256.256:1"
			Expect(a.Start(false)).To(HaveOccurred())
		})
	})

	Describe("RequireToken challenge", func() {
		It("should advertise the Bearer scheme on 401", func() {
			rec := httptest.NewRecorder()
			api.RequireToken(testHandler)(rec, httptest.NewRequest("GET", "/hello", nil))
			Expect(rec.Header().Get("WWW-Authenticate")).To(Equal("Bearer"))
		})
		It("should reject a token that only shares a prefix with the real one", func() {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/hello", nil)
			req.Header.Set("Authorization", "Bearer "+token+"x")
			api.RequireToken(testHandler)(rec, req)
			Expect(rec.Code).To(Equal(http.StatusUnauthorized))
		})
		It("should serve registered handlers on its own mux", func() {
			a := New(token)
			a.RegisterFunc("/hello", testHandler)
			Expect(a.mux).NotTo(BeNil())
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("GET", "/hello", nil)
			req.Header.Set("Authorization", "Bearer "+token)
			a.mux.ServeHTTP(rec, req)
			Expect(rec.Code).To(Equal(http.StatusOK))
			Expect(rec.Body.String()).To(Equal("Hello!"))
		})
	})
})

func testHandler(w http.ResponseWriter, req *http.Request) {
	_, _ = io.WriteString(w, "Hello!")
}
