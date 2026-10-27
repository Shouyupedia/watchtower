package auth_test

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shouyupedia/watchtower/internal/actions/mocks"
	"github.com/shouyupedia/watchtower/pkg/registry/auth"
	"github.com/shouyupedia/watchtower/pkg/registry/httpclient"

	ref "github.com/distribution/reference"
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	wtTypes "github.com/shouyupedia/watchtower/pkg/types"
)

func TestAuth(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Registry Auth Suite")
}
func SkipIfCredentialsEmpty(credentials *wtTypes.RegistryCredentials, fn func()) func() {
	if credentials.Username == "" {
		return func() {
			Skip("Username missing. Skipping integration test")
		}
	} else if credentials.Password == "" {
		return func() {
			Skip("Password missing. Skipping integration test")
		}
	} else {
		return fn
	}
}

var GHCRCredentials = &wtTypes.RegistryCredentials{
	Username: os.Getenv("CI_INTEGRATION_TEST_REGISTRY_GH_USERNAME"),
	Password: os.Getenv("CI_INTEGRATION_TEST_REGISTRY_GH_PASSWORD"),
}

var _ = Describe("the auth module", func() {
	mockId := "mock-id"
	mockName := "mock-container"
	mockImage := "ghcr.io/k6io/operator:latest"
	mockCreated := time.Now()
	mockDigest := "ghcr.io/k6io/operator@sha256:d68e1e532088964195ad3a0a71526bc2f11a78de0def85629beb75e2265f0547"

	mockContainer := mocks.CreateMockContainerWithDigest(
		mockId,
		mockName,
		mockImage,
		mockCreated,
		mockDigest)

	Describe("GetToken", func() {
		It("should parse the token from the response",
			SkipIfCredentialsEmpty(GHCRCredentials, func() {
				creds := fmt.Sprintf("%s:%s", GHCRCredentials.Username, GHCRCredentials.Password)
				token, err := auth.GetToken(context.Background(), mockContainer, creds)
				Expect(err).NotTo(HaveOccurred())
				Expect(token).NotTo(Equal(""))
			}),
		)
	})

	Describe("GetAuthURL", func() {
		It("should create a valid auth url object based on the challenge header supplied", func() {
			challenge := `bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:user/image:pull"`
			imageRef, err := ref.ParseNormalizedNamed("containrrr/watchtower")
			Expect(err).NotTo(HaveOccurred())
			expected := &url.URL{
				Host:     "ghcr.io",
				Scheme:   "https",
				Path:     "/token",
				RawQuery: "scope=repository%3Acontainrrr%2Fwatchtower%3Apull&service=ghcr.io",
			}

			URL, err := auth.GetAuthURL(challenge, imageRef)
			Expect(err).NotTo(HaveOccurred())
			Expect(URL).To(Equal(expected))
		})

		When("given an invalid challenge header", func() {
			It("should return an error", func() {
				challenge := `bearer realm="https://ghcr.io/token"`
				imageRef, err := ref.ParseNormalizedNamed("containrrr/watchtower")
				Expect(err).NotTo(HaveOccurred())
				URL, err := auth.GetAuthURL(challenge, imageRef)
				Expect(err).To(HaveOccurred())
				Expect(URL).To(BeNil())
			})
		})

		When("deriving the auth scope from an image name", func() {
			It("should prepend official dockerhub images with \"library/\"", func() {
				Expect(getScopeFromImageAuthURL("registry")).To(Equal("library/registry"))
				Expect(getScopeFromImageAuthURL("docker.io/registry")).To(Equal("library/registry"))
				Expect(getScopeFromImageAuthURL("index.docker.io/registry")).To(Equal("library/registry"))
			})
			It("should not include vanity hosts\"", func() {
				Expect(getScopeFromImageAuthURL("docker.io/containrrr/watchtower")).To(Equal("containrrr/watchtower"))
				Expect(getScopeFromImageAuthURL("index.docker.io/containrrr/watchtower")).To(Equal("containrrr/watchtower"))
			})
			It("should not destroy three segment image names\"", func() {
				Expect(getScopeFromImageAuthURL("piksel/containrrr/watchtower")).To(Equal("piksel/containrrr/watchtower"))
				Expect(getScopeFromImageAuthURL("ghcr.io/piksel/containrrr/watchtower")).To(Equal("piksel/containrrr/watchtower"))
			})
			It("should not prepend library/ to image names if they're not on dockerhub", func() {
				Expect(getScopeFromImageAuthURL("ghcr.io/watchtower")).To(Equal("watchtower"))
				Expect(getScopeFromImageAuthURL("ghcr.io/containrrr/watchtower")).To(Equal("containrrr/watchtower"))
			})
		})
		It("should not crash when an empty field is received", func() {
			input := `bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:user/image:pull",`
			imageRef, err := ref.ParseNormalizedNamed("containrrr/watchtower")
			Expect(err).NotTo(HaveOccurred())
			res, err := auth.GetAuthURL(input, imageRef)
			Expect(err).NotTo(HaveOccurred())
			Expect(res).NotTo(BeNil())
		})
		It("should not crash when a field without a value is received", func() {
			input := `bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:user/image:pull",valuelesskey`
			imageRef, err := ref.ParseNormalizedNamed("containrrr/watchtower")
			Expect(err).NotTo(HaveOccurred())
			res, err := auth.GetAuthURL(input, imageRef)
			Expect(err).NotTo(HaveOccurred())
			Expect(res).NotTo(BeNil())
		})
	})

	Describe("GetAuthURL hardening", func() {
		imageRef, _ := ref.ParseNormalizedNamed("ghcr.io/user/image")

		It("should preserve the case of the realm path", func() {
			challenge := `Bearer realm="https://auth.example.com/Token/V2",service="Registry"`
			URL, err := auth.GetAuthURL(challenge, imageRef)
			Expect(err).NotTo(HaveOccurred())
			Expect(URL.Path).To(Equal("/Token/V2"))
			Expect(URL.Query().Get("service")).To(Equal("Registry"))
		})
		It("should reject a realm that is not served over https", func() {
			challenge := `Bearer realm="http://auth.example.com/token",service="registry"`
			URL, err := auth.GetAuthURL(challenge, imageRef)
			Expect(err).To(HaveOccurred())
			Expect(URL).To(BeNil())
		})
		It("should return an error instead of crashing on a malformed realm", func() {
			challenge := `Bearer realm="://%zz",service="registry"`
			Expect(func() { _, _ = auth.GetAuthURL(challenge, imageRef) }).NotTo(Panic())
			_, err := auth.GetAuthURL(challenge, imageRef)
			Expect(err).To(HaveOccurred())
		})
		It("should reject a realm without a host", func() {
			_, err := auth.GetAuthURL(`Bearer realm="/token",service="registry"`, imageRef)
			Expect(err).To(HaveOccurred())
		})
	})

	Describe("GetBearerHeader", func() {
		imageRef, _ := ref.ParseNormalizedNamed("ghcr.io/user/image")
		var server *httptest.Server
		BeforeEach(func() {
			auth.ResetCache()
		})
		AfterEach(func() {
			httpclient.SetInsecureSkipVerify(false)
			if server != nil {
				server.Close()
			}
		})

		challengeFor := func(s *httptest.Server) string {
			return fmt.Sprintf(`Bearer realm="%s/token",service="registry"`, s.URL)
		}

		It("should fetch a token and accept the access_token field", func() {
			server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(`{"access_token":"abc"}`))
			}))
			httpclient.SetInsecureSkipVerify(true)
			header, err := auth.GetBearerHeader(context.Background(), challengeFor(server), imageRef, "")
			Expect(err).NotTo(HaveOccurred())
			Expect(header).To(Equal("Bearer abc"))
		})
		It("should not send credentials to a token server with an untrusted certificate", func() {
			var hits atomic.Int32
			server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				hits.Add(1)
				_, _ = w.Write([]byte(`{"token":"abc"}`))
			}))
			_, err := auth.GetBearerHeader(context.Background(), challengeFor(server), imageRef, "dXNlcjpwYXNz")
			Expect(err).To(HaveOccurred())
			Expect(hits.Load()).To(BeZero())
		})
		It("should fail on a non-200 response or an empty token", func() {
			server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Query().Get("service") == "registry" {
					w.WriteHeader(http.StatusUnauthorized)
				}
			}))
			httpclient.SetInsecureSkipVerify(true)
			_, err := auth.GetBearerHeader(context.Background(), challengeFor(server), imageRef, "")
			Expect(err).To(HaveOccurred())
		})
	})

	Describe("GetChallengeURL", func() {
		It("should create a valid challenge url object based on the image ref supplied", func() {
			expected := url.URL{Host: "ghcr.io", Scheme: "https", Path: "/v2/"}
			imageRef, _ := ref.ParseNormalizedNamed("ghcr.io/containrrr/watchtower:latest")
			Expect(auth.GetChallengeURL(imageRef)).To(Equal(expected))
		})
		It("should assume Docker Hub for image refs with no explicit registry", func() {
			expected := url.URL{Host: "index.docker.io", Scheme: "https", Path: "/v2/"}
			imageRef, _ := ref.ParseNormalizedNamed("containrrr/watchtower:latest")
			Expect(auth.GetChallengeURL(imageRef)).To(Equal(expected))
		})
		It("should use index.docker.io if the image ref specifies docker.io", func() {
			expected := url.URL{Host: "index.docker.io", Scheme: "https", Path: "/v2/"}
			imageRef, _ := ref.ParseNormalizedNamed("docker.io/containrrr/watchtower:latest")
			Expect(auth.GetChallengeURL(imageRef)).To(Equal(expected))
		})
	})
})

var scopeImageRegexp = MatchRegexp("^repository:[a-z0-9]+(/[a-z0-9]+)*:pull$")

func getScopeFromImageAuthURL(imageName string) string {
	normalizedRef, _ := ref.ParseNormalizedNamed(imageName)
	challenge := `bearer realm="https://dummy.host/token",service="dummy.host",scope="repository:user/image:pull"`
	URL, _ := auth.GetAuthURL(challenge, normalizedRef)

	scope := URL.Query().Get("scope")
	Expect(scopeImageRegexp.Match(scope)).To(BeTrue())
	return strings.Replace(scope[11:], ":pull", "", 1)
}
