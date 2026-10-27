package types

import (
	"time"
)

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
	// CheckConcurrency is the number of containers that are checked for new images in parallel (1 or less: one by one)
	CheckConcurrency int
}
