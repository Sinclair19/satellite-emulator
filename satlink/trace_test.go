package main

import (
	"os"
	"testing"
	"time"
)

func TestReferenceSwitch(t *testing.T) {
	data, err := os.ReadFile("../examples/transparent-sn.json")
	if err != nil {
		t.Fatal(err)
	}
	p, err := parseTrace(data)
	if err != nil {
		t.Fatal(err)
	}
	if len(p) != 5 || p[1].DelayMS != 8.25 || !p[2].down() || p[3].down() || p[3].At-p[2].At != 80*time.Millisecond || p[3].Satellite != "STARLINK-181" || p[4].DelayMS != 0 {
		t.Fatal(p)
	}
}

func TestInheritedStateAndExplicitZero(t *testing.T) {
	p, err := parseTrace([]byte(`[{"time":0,"metadata":{"run":"test"},"delay_ue_ran":3,"delay_ran_5g":6,"satellite":"A"},
		{"time":1,"delay_ue_ran":0},{"time":1,"satellite":"B"},{"time":2,"pdr":0},
		{"time":3,"event":"handover_end","pdr":0.5,"delay_ms":0}]`))
	if err != nil {
		t.Fatal(err)
	}
	if len(p) != 4 || p[1].DelayMS != 6 || p[1].down() || p[1].Satellite != "B" || !p[2].down() || p[3].PDR != 0.5 || p[3].DelayMS != 0 {
		t.Fatal(p)
	}
}

func TestInvalidTrace(t *testing.T) {
	for _, data := range []string{
		`[]`, `[{}]`, `[{"time":1}]`, `[{"time":0},{"time":-1}]`,
		`[{"time":0,"event":"release_gnb"}]`, `[{"time":0,"event":"link_up","link_available":false}]`,
		`[{"time":0,"delay_ms":1,"delay_ran_5g":1}]`, `[{"time":0,"delay_ms":-1}]`,
		`[{"time":0,"pdr":1.01}]`, `[{"time":0,"pdr":0},{"time":1,"event":"link_up"}]`,
	} {
		if _, err := parseTrace([]byte(data)); err == nil {
			t.Errorf("accepted %s", data)
		}
	}
}
