package manifest

import (
	"errors"
	"fmt"
	url2 "net/url"

	ref "github.com/distribution/reference"
	log "github.com/shouyupedia/watchtower/internal/logging"
	"github.com/shouyupedia/watchtower/pkg/registry/helpers"
	"github.com/shouyupedia/watchtower/pkg/types"
)

// BuildManifestURL from raw image data
func BuildManifestURL(container types.Container) (string, error) {
	normalizedRef, err := ref.ParseDockerRef(container.ImageName())
	if err != nil {
		return "", err
	}
	normalizedTaggedRef, isTagged := normalizedRef.(ref.NamedTagged)
	if !isTagged {
		return "", errors.New("Parsed container image ref has no tag: " + normalizedRef.String())
	}

	host, _ := helpers.GetRegistryAddress(normalizedTaggedRef.Name())
	img, tag := ref.Path(normalizedTaggedRef), normalizedTaggedRef.Tag()

	log.WithFields(log.Fields{
		"image":      img,
		"tag":        tag,
		"normalized": normalizedTaggedRef.Name(),
		"host":       host,
	}).Debug("Parsing image ref")

	if err != nil {
		return "", err
	}

	url := url2.URL{
		Scheme: "https",
		Host:   host,
		Path:   fmt.Sprintf("/v2/%s/manifests/%s", img, tag),
	}
	return url.String(), nil
}
