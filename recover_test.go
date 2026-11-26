package main

import (
	"testing"

	"go.etcd.io/etcd/api/v3/mvccpb"
	clientv3 "go.etcd.io/etcd/client/v3"
)

func TestApplyRecoveryEventsPutAndDelete(t *testing.T) {
	state := &recoveryState{
		SchemaVersion: recoveryStateVersion,
		Prefix:        "/watchlab/test/",
		LastRevision:  10,
		Keys: map[string]recoveryKV{
			"/watchlab/test/old": {Value: "old", ModRevision: 9},
		},
	}
	events := []*clientv3.Event{
		{
			Type: mvccpb.PUT,
			Kv: &mvccpb.KeyValue{
				Key:            []byte("/watchlab/test/new"),
				Value:          []byte("new-value"),
				CreateRevision: 11,
				ModRevision:    11,
				Version:        1,
			},
		},
		{
			Type: mvccpb.DELETE,
			Kv: &mvccpb.KeyValue{
				Key:         []byte("/watchlab/test/old"),
				ModRevision: 12,
			},
		},
	}

	records, err := applyRecoveryEvents(state, events, "test-run")
	if err != nil {
		t.Fatalf("applyRecoveryEvents returned error: %v", err)
	}
	if len(records) != 2 {
		t.Fatalf("got %d records, want 2", len(records))
	}
	if state.LastRevision != 12 {
		t.Fatalf("LastRevision = %d, want 12", state.LastRevision)
	}
	if got := state.Keys["/watchlab/test/new"].Value; got != "new-value" {
		t.Fatalf("new key value = %q, want %q", got, "new-value")
	}
	if _, ok := state.Keys["/watchlab/test/old"]; ok {
		t.Fatal("deleted key remains in state")
	}
}

func TestApplyRecoveryEventsKeepsCheckpointMonotonic(t *testing.T) {
	state := &recoveryState{
		SchemaVersion: recoveryStateVersion,
		Prefix:        "/watchlab/test/",
		LastRevision:  50,
		Keys:          map[string]recoveryKV{},
	}
	events := []*clientv3.Event{{
		Type: mvccpb.PUT,
		Kv: &mvccpb.KeyValue{
			Key:         []byte("/watchlab/test/key"),
			Value:       []byte("value"),
			ModRevision: 49,
		},
	}}
	if _, err := applyRecoveryEvents(state, events, "test-run"); err != nil {
		t.Fatalf("applyRecoveryEvents returned error: %v", err)
	}
	if state.LastRevision != 50 {
		t.Fatalf("LastRevision moved backwards to %d", state.LastRevision)
	}
}
