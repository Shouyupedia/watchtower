package types

import (
	"time"
)

// UpdateStrategy determines how a container is replaced with its updated version
type UpdateStrategy string

const (
	// StrategyRecreate removes the old container before creating the new one
	StrategyRecreate UpdateStrategy = "recreate"
	// StrategyReplace keeps the old container (stopped and renamed) until the new one is confirmed to work,
	// and puts it back if it is not
	StrategyReplace UpdateStrategy = "replace"
)

// ParseUpdateStrategy converts the value of the update strategy option
func ParseUpdateStrategy(value string) (UpdateStrategy, bool) {
	switch UpdateStrategy(value) {
	case "", StrategyRecreate:
		return StrategyRecreate, true
	case StrategyReplace:
		return StrategyReplace, true
	}
	return "", false
}

// UpdateParams contains all different options available to alter the behavior of the Update func
type UpdateParams struct {
	Filter          Filter
	Cleanup         bool
	NoRestart       bool
	Timeout         time.Duration
	MonitorOnly     bool
	NoPull          bool
	LifecycleHooks  bool
	RollingRestart  bool
	LabelPrecedence bool
	// UpdateStrategy is how containers are replaced, the zero value being StrategyRecreate
	UpdateStrategy UpdateStrategy
	// HealthTimeout is for how long a replaced container has to prove itself before the old one is removed (0: not at all)
	HealthTimeout time.Duration
	// RollbackOnFailure recreates a container from its previous image if it could not be started with the new one
	RollbackOnFailure bool
	// CheckConcurrency is the number of containers that are checked for new images in parallel (1 or less: one by one)
	CheckConcurrency int
}
