package actions

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/shouyupedia/watchtower/pkg/container"
	"github.com/shouyupedia/watchtower/pkg/filters"
	"github.com/shouyupedia/watchtower/pkg/sorter"
	"github.com/shouyupedia/watchtower/pkg/types"

	log "github.com/shouyupedia/watchtower/internal/logging"
)

// CheckForSanity makes sure everything is sane before starting
func CheckForSanity(ctx context.Context, client container.Client, filter types.Filter, rollingRestarts bool) error {
	log.Debug("Making sure everything is sane before starting")

	if rollingRestarts {
		containers, err := client.ListContainers(ctx, filter)
		if err != nil {
			return err
		}
		for _, c := range containers {
			if len(c.Links()) > 0 {
				return fmt.Errorf(
					"%q is depending on at least one other container. This is not compatible with rolling restarts",
					c.Name(),
				)
			}
		}
	}
	return nil
}

// CheckForMultipleWatchtowerInstances will ensure that there are not multiple instances of the
// watchtower running simultaneously. If multiple watchtower containers are detected, this function
// will stop and remove all but the most recently started container. This behaviour can be bypassed
// if a scope UID is defined.
func CheckForMultipleWatchtowerInstances(ctx context.Context, client container.Client, cleanup bool, scope string) error {
	filter := filters.WatchtowerContainersFilter
	if scope != "" {
		filter = filters.FilterByScope(scope, filter)
	}
	containers, err := client.ListContainers(ctx, filter)

	if err != nil {
		return err
	}

	if len(containers) <= 1 {
		log.Debug("There are no additional watchtower containers")
		return nil
	}

	log.Info("Found multiple running watchtower instances. Cleaning up.")
	return cleanupExcessWatchtowers(ctx, containers, client, cleanup)
}

func cleanupExcessWatchtowers(ctx context.Context, containers []types.Container, client container.Client, cleanup bool) error {
	var stopErrors int

	sort.Sort(sorter.ByCreated(containers))
	allContainersExceptLast := containers[0 : len(containers)-1]

	for _, c := range allContainersExceptLast {
		if err := client.StopContainer(ctx, c, 10*time.Minute); err != nil {
			// logging the original here as we're just returning a count
			log.WithError(err).Error("Could not stop a previous watchtower instance.")
			stopErrors++
			continue
		}

		if cleanup {
			if err := client.RemoveImageByID(ctx, c.ImageID()); err != nil {
				log.WithError(err).Warn("Could not cleanup watchtower images, possibly because of other watchtowers instances in other scopes.")
			}
		}
	}

	if stopErrors > 0 {
		return fmt.Errorf("%d errors while stopping watchtower containers", stopErrors)
	}

	return nil
}
