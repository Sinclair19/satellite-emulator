// Adapted from freeRANUE's ntn/trace.go. See ../docs/sources/free-ran-ue-LICENSE.
package main

import (
	"encoding/json"
	"fmt"
	"math"
	"time"
)

type event struct {
	Time      *float64 `json:"time"`
	Satellite string   `json:"satellite"`
	DelayMS   *float64 `json:"delay_ms"`
	ServiceMS *float64 `json:"delay_ue_ran"`
	FeederMS  *float64 `json:"delay_ran_5g"`
	PDR       *float64 `json:"pdr"`
	Available *bool    `json:"link_available"`
	Event     string   `json:"event"`
}

// point is the effective state after all updates at one instant.
type point struct {
	At        time.Duration `json:"time_ns"`
	Satellite string        `json:"satellite"`
	DelayMS   float64       `json:"delay_ms"`
	PDR       float64       `json:"pdr"`
	Available bool          `json:"link_available"`
}

func (p point) down() bool { return !p.Available || p.PDR == 0 }

func parseTrace(data []byte) ([]point, error) {
	var events []event
	if err := json.Unmarshal(data, &events); err != nil {
		return nil, err
	}
	if len(events) == 0 {
		return nil, fmt.Errorf("empty timeline")
	}
	var points []point
	var service, feeder float64
	state := point{Available: true, PDR: 1}
	for i, e := range events {
		if e.Time == nil || *e.Time < 0 || *e.Time*1e9 >= float64(math.MaxInt64) {
			return nil, fmt.Errorf("event %d: invalid or missing time", i)
		}
		at := time.Duration(math.Round(*e.Time * 1e9))
		if (i == 0 && at != 0) || at < state.At {
			return nil, fmt.Errorf("event %d: timeline must start at zero and be ordered", i)
		}
		state.At = at
		switch e.Event {
		case "":
		case "link_down", "handover_start":
			state.Available = false
		case "link_up", "handover_end":
			state.Available = true
			if state.PDR == 0 && (e.PDR == nil || *e.PDR <= 0) {
				return nil, fmt.Errorf("event %d: recovery from zero PDR requires positive PDR", i)
			}
		default:
			return nil, fmt.Errorf("event %d: unknown event %q", i, e.Event)
		}
		if e.Available != nil {
			if e.Event != "" && *e.Available != state.Available {
				return nil, fmt.Errorf("event %d: inconsistent availability", i)
			}
			state.Available = *e.Available
		}
		if e.Satellite != "" {
			state.Satellite = e.Satellite
		}
		if e.PDR != nil {
			if *e.PDR < 0 || *e.PDR > 1 {
				return nil, fmt.Errorf("event %d: PDR outside [0,1]", i)
			}
			state.PDR = *e.PDR
		}
		if e.DelayMS != nil && (e.ServiceMS != nil || e.FeederMS != nil) {
			return nil, fmt.Errorf("event %d: mixed total and component delays", i)
		}
		for _, v := range []*float64{e.DelayMS, e.ServiceMS, e.FeederMS} {
			if v != nil && *v < 0 {
				return nil, fmt.Errorf("event %d: negative delay", i)
			}
		}
		if e.DelayMS != nil {
			service, feeder = *e.DelayMS, 0
		}
		if e.ServiceMS != nil {
			service = *e.ServiceMS
		}
		if e.FeederMS != nil {
			feeder = *e.FeederMS
		}
		state.DelayMS = service + feeder
		if len(points) > 0 && points[len(points)-1].At == at {
			points[len(points)-1] = state
		} else {
			points = append(points, state)
		}
	}
	return points, nil
}
