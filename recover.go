package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const recoveryStateVersion = 1

type recoveryKV struct {
	Value          string `json:"value"`
	CreateRevision int64  `json:"create_revision,omitempty"`
	ModRevision    int64  `json:"mod_revision,omitempty"`
	Version        int64  `json:"version,omitempty"`
	Lease          int64  `json:"lease,omitempty"`
}

type recoveryState struct {
	SchemaVersion      int                   `json:"schema_version"`
	Prefix             string                `json:"prefix"`
	SnapshotRevision   int64                 `json:"snapshot_revision"`
	LastRevision       int64                 `json:"last_revision"`
	UpdatedAt          string                `json:"updated_at"`
	SnapshotDurationMs float64               `json:"snapshot_duration_ms,omitempty"`
	DataBytes          int64                 `json:"data_bytes,omitempty"`
	Keys               map[string]recoveryKV `json:"keys"`
}

type recoveryRecord struct {
	Type             string  `json:"type"`
	RunID            string  `json:"run_id"`
	Timestamp        string  `json:"timestamp"`
	Prefix           string  `json:"prefix,omitempty"`
	StartRevision    int64   `json:"start_revision,omitempty"`
	SnapshotRevision int64   `json:"snapshot_revision,omitempty"`
	LastRevision     int64   `json:"last_revision,omitempty"`
	KeyCount         int     `json:"key_count,omitempty"`
	DataBytes        int64   `json:"data_bytes,omitempty"`
	DurationMs       float64 `json:"duration_ms,omitempty"`
	EventRevision    int64   `json:"event_revision,omitempty"`
	EventType        string  `json:"event_type,omitempty"`
	Key              string  `json:"key,omitempty"`
	Value            string  `json:"value,omitempty"`
	CompactRevision  int64   `json:"compact_revision,omitempty"`
	Error            string  `json:"error,omitempty"`
}

func runRecover(args []string) error {
	fs := flag.NewFlagSet("recover", flag.ContinueOnError)
	endpoint := fs.String("endpoint", defaultEndpoint, "etcd client endpoint")
	runID := fs.String("run-id", "recover-001", "experiment ID")
	prefix := fs.String("prefix", "", "key prefix to snapshot and watch; defaults to /watchlab/runs/<run-id>/events/")
	out := fs.String("out", "", "JSONL recovery log; defaults to results/<run-id>-recover.jsonl")
	statePath := fs.String("state", "", "persistent local state file; defaults to results/<run-id>-state.json")
	requestTimeout := fs.Duration("request-timeout", 15*time.Second, "timeout for each Range snapshot request")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := validateRunID(*runID); err != nil {
		return err
	}
	if *requestTimeout <= 0 {
		return errors.New("--request-timeout must be positive")
	}
	if *prefix == "" {
		*prefix = defaultPrefix(*runID)
	}
	if !strings.HasSuffix(*prefix, "/") {
		return errors.New("--prefix must end with '/'")
	}
	if *out == "" {
		*out = filepath.Join("results", *runID+"-recover.jsonl")
	}
	if *statePath == "" {
		*statePath = filepath.Join("results", *runID+"-state.json")
	}
	if filepath.Clean(*out) == filepath.Clean(*statePath) {
		return errors.New("--out and --state must refer to different files")
	}

	logFile, encoder, err := createJSONL(*out)
	if err != nil {
		return err
	}
	defer logFile.Close()

	cli, err := newClient(*endpoint)
	if err != nil {
		return err
	}
	defer cli.Close()

	ctx, stop := signalContext()
	defer stop()

	state, err := loadRecoveryState(*statePath, *prefix)
	if err != nil {
		return err
	}
	if state == nil {
		state, err = takeRecoverySnapshot(ctx, cli, *prefix, *requestTimeout)
		if err != nil {
			return err
		}
		if err := saveRecoveryState(*statePath, state); err != nil {
			return fmt.Errorf("save initial recovery state: %w", err)
		}
		if err := writeSnapshotRecord(encoder, *runID, *prefix, state, 0); err != nil {
			return err
		}
		fmt.Printf("RECOVER_SNAPSHOT revision=%d keys=%d state=%s\n", state.SnapshotRevision, len(state.Keys), *statePath)
	} else {
		fmt.Printf("RECOVER_STATE_LOADED revision=%d keys=%d state=%s\n", state.LastRevision, len(state.Keys), *statePath)
	}

	fmt.Printf("RECOVER_START run_id=%s prefix=%s state=%s output=%s\n", *runID, *prefix, *statePath, *out)
	for {
		if ctx.Err() != nil {
			fmt.Printf("RECOVER_STOPPED reason=%v last_revision=%d\n", ctx.Err(), state.LastRevision)
			return nil
		}
		if state.LastRevision == int64(^uint64(0)>>1) {
			return errors.New("last revision reached maximum int64 value")
		}
		startRevision := state.LastRevision + 1
		fmt.Printf("RECOVER_WATCH_START start_revision=%d\n", startRevision)
		watchCh := cli.Watch(
			ctx,
			*prefix,
			clientv3.WithPrefix(),
			clientv3.WithCreatedNotify(),
			clientv3.WithProgressNotify(),
			clientv3.WithRev(startRevision),
		)

		created := false
		resnapshot := false
		for {
			select {
			case <-ctx.Done():
				fmt.Printf("RECOVER_STOPPED reason=%v last_revision=%d\n", ctx.Err(), state.LastRevision)
				return nil
			case response, ok := <-watchCh:
				if !ok {
					if ctx.Err() != nil {
						fmt.Printf("RECOVER_STOPPED reason=%v last_revision=%d\n", ctx.Err(), state.LastRevision)
						return nil
					}
					return errors.New("recovery watch channel closed unexpectedly")
				}

				if response.Canceled {
					reason := "watch canceled without a reported error"
					if watchErr := response.Err(); watchErr != nil {
						reason = watchErr.Error()
					}
					record := recoveryRecord{
						Type:            "watch_canceled",
						RunID:           *runID,
						Timestamp:       time.Now().UTC().Format(time.RFC3339Nano),
						LastRevision:    state.LastRevision,
						CompactRevision: response.CompactRevision,
						Error:           reason,
					}
					if err := encoder.Encode(record); err != nil {
						return fmt.Errorf("write canceled record: %w", err)
					}
					if response.CompactRevision <= 0 && !strings.Contains(strings.ToLower(reason), "compacted") {
						return fmt.Errorf("recovery watch canceled: %s", reason)
					}

					fmt.Printf("RECOVER_COMPACTED compact_revision=%d last_revision=%d; taking a new snapshot\n", response.CompactRevision, state.LastRevision)
					newState, snapshotErr := takeRecoverySnapshot(ctx, cli, *prefix, *requestTimeout)
					if snapshotErr != nil {
						return fmt.Errorf("take recovery snapshot after compaction: %w", snapshotErr)
					}
					state = newState
					if err := saveRecoveryState(*statePath, state); err != nil {
						return fmt.Errorf("save state after compaction: %w", err)
					}
					if err := writeSnapshotRecord(encoder, *runID, *prefix, state, response.CompactRevision); err != nil {
						return err
					}
					fmt.Printf("RECOVER_SNAPSHOT revision=%d keys=%d state=%s\n", state.SnapshotRevision, len(state.Keys), *statePath)
					resnapshot = true
					break
				}

				if watchErr := response.Err(); watchErr != nil {
					return fmt.Errorf("recovery watch response: %w", watchErr)
				}
				if response.Created {
					created = true
					record := recoveryRecord{
						Type:          "watch_created",
						RunID:         *runID,
						Timestamp:     time.Now().UTC().Format(time.RFC3339Nano),
						StartRevision: startRevision,
					}
					if response.Header != nil {
						record.LastRevision = response.Header.Revision
					}
					if err := encoder.Encode(record); err != nil {
						return fmt.Errorf("write watch-created record: %w", err)
					}
					fmt.Printf("RECOVER_WATCH_CREATED start_revision=%d\n", startRevision)
					continue
				}
				if !created {
					fmt.Fprintln(os.Stderr, "warning: recovery received a Watch response before created notification")
				}

				if len(response.Events) == 0 {
					// WithProgressNotify, an empty response advances the safe watch
					// checkpoint: etcd has delivered all matching events through Header.Revision.
					if response.IsProgressNotify() && response.Header != nil && response.Header.Revision > state.LastRevision {
						previous := state.LastRevision
						state.LastRevision = response.Header.Revision
						state.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
						if err := saveRecoveryState(*statePath, state); err != nil {
							return fmt.Errorf("save progress checkpoint: %w", err)
						}
						record := recoveryRecord{
							Type:         "watch_progress",
							RunID:        *runID,
							Timestamp:    state.UpdatedAt,
							LastRevision: state.LastRevision,
						}
						if err := encoder.Encode(record); err != nil {
							return fmt.Errorf("write progress record: %w", err)
						}
						fmt.Printf("RECOVER_PROGRESS revision=%d (advanced from %d)\n", state.LastRevision, previous)
					}
					continue
				}

				records, err := applyRecoveryEvents(state, response.Events, *runID)
				if err != nil {
					return err
				}
				// Journal the complete Watch response before advancing the durable
				// checkpoint. A crash between these steps can cause duplicate log
				// records on replay, but cannot silently omit an acknowledged event.
				for _, record := range records {
					if err := encoder.Encode(record); err != nil {
						return fmt.Errorf("write recovery event record: %w", err)
					}
					fmt.Printf("RECOVER_EVENT type=%s revision=%d key=%s\n", record.EventType, record.EventRevision, record.Key)
				}
				if err := logFile.Sync(); err != nil {
					return fmt.Errorf("sync recovery event log: %w", err)
				}
				// State and checkpoint live in the same atomically replaced file.
				// If the process stops before this save, events are replayed and
				// may appear again in the journal; PUT/DELETE state application is
				// idempotent for this key-value cache.
				if err := saveRecoveryState(*statePath, state); err != nil {
					return fmt.Errorf("save state after Watch response: %w", err)
				}
			}
			if resnapshot {
				break
			}
		}
	}
}

func takeRecoverySnapshot(ctx context.Context, cli *clientv3.Client, prefix string, timeout time.Duration) (*recoveryState, error) {
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	started := time.Now()
	response, err := cli.Get(requestCtx, prefix, clientv3.WithPrefix())
	duration := time.Since(started)
	if err != nil {
		return nil, fmt.Errorf("Range snapshot for prefix %q: %w", prefix, err)
	}
	if response.Header == nil {
		return nil, errors.New("Range snapshot returned no response header")
	}

	state := &recoveryState{
		SchemaVersion:    recoveryStateVersion,
		Prefix:           prefix,
		SnapshotRevision: response.Header.Revision,
		LastRevision:     response.Header.Revision,
		UpdatedAt:        time.Now().UTC().Format(time.RFC3339Nano),
		Keys:             make(map[string]recoveryKV, len(response.Kvs)),
	}
	var dataBytes int64
	for _, kv := range response.Kvs {
		if kv == nil {
			continue
		}
		key := string(kv.Key)
		state.Keys[key] = recoveryKV{
			Value:          string(kv.Value),
			CreateRevision: kv.CreateRevision,
			ModRevision:    kv.ModRevision,
			Version:        kv.Version,
			Lease:          kv.Lease,
		}
		dataBytes += int64(len(kv.Key) + len(kv.Value))
	}
	state.SnapshotDurationMs = float64(duration.Nanoseconds()) / 1e6
	state.DataBytes = dataBytes
	return state, nil
}

func writeSnapshotRecord(encoder *json.Encoder, runID, prefix string, state *recoveryState, compactRevision int64) error {
	dataBytes := state.DataBytes
	if dataBytes == 0 && len(state.Keys) > 0 {
		for key, value := range state.Keys {
			dataBytes += int64(len(key) + len(value.Value))
		}
	}
	record := recoveryRecord{
		Type:             "snapshot",
		RunID:            runID,
		Timestamp:        time.Now().UTC().Format(time.RFC3339Nano),
		Prefix:           prefix,
		SnapshotRevision: state.SnapshotRevision,
		LastRevision:     state.LastRevision,
		KeyCount:         len(state.Keys),
		DataBytes:        dataBytes,
		DurationMs:       state.SnapshotDurationMs,
		CompactRevision:  compactRevision,
	}
	if err := encoder.Encode(record); err != nil {
		return fmt.Errorf("write snapshot record: %w", err)
	}
	return nil
}

func loadRecoveryState(path, prefix string) (*recoveryState, error) {
	file, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("open recovery state %q: %w", path, err)
	}
	defer file.Close()

	var state recoveryState
	if err := json.NewDecoder(file).Decode(&state); err != nil {
		return nil, fmt.Errorf("decode recovery state %q: %w", path, err)
	}
	if state.SchemaVersion != recoveryStateVersion {
		return nil, fmt.Errorf("unsupported recovery state schema %d in %q", state.SchemaVersion, path)
	}
	if state.Prefix != prefix {
		return nil, fmt.Errorf("state prefix %q does not match requested prefix %q", state.Prefix, prefix)
	}
	if state.LastRevision < state.SnapshotRevision || state.LastRevision < 0 {
		return nil, fmt.Errorf("invalid recovery checkpoint: snapshot_revision=%d last_revision=%d", state.SnapshotRevision, state.LastRevision)
	}
	if state.Keys == nil {
		state.Keys = make(map[string]recoveryKV)
	}
	return &state, nil
}

func saveRecoveryState(path string, state *recoveryState) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create state directory: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temporary state file: %w", err)
	}
	tmpName := tmp.Name()
	cleanup := func() {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
	}

	encoder := json.NewEncoder(tmp)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(state); err != nil {
		cleanup()
		return fmt.Errorf("encode state: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		cleanup()
		return fmt.Errorf("sync temporary state: %w", err)
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		return fmt.Errorf("close temporary state: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		_ = os.Remove(tmpName)
		return fmt.Errorf("replace recovery state: %w", err)
	}
	dirFile, err := os.Open(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("open state directory after rename: %w", err)
	}
	defer dirFile.Close()
	if err := dirFile.Sync(); err != nil {
		return fmt.Errorf("sync state directory after rename: %w", err)
	}
	return nil
}

func applyRecoveryEvents(state *recoveryState, events []*clientv3.Event, runID string) ([]recoveryRecord, error) {
	if state.Keys == nil {
		state.Keys = make(map[string]recoveryKV)
	}
	records := make([]recoveryRecord, 0, len(events))
	maxRevision := state.LastRevision
	now := time.Now().UTC().Format(time.RFC3339Nano)

	for _, event := range events {
		if event == nil || event.Kv == nil {
			continue
		}
		key := string(event.Kv.Key)
		eventType := event.Type.String()
		value := string(event.Kv.Value)
		switch event.Type {
		case clientv3.EventTypePut:
			state.Keys[key] = recoveryKV{
				Value:          value,
				CreateRevision: event.Kv.CreateRevision,
				ModRevision:    event.Kv.ModRevision,
				Version:        event.Kv.Version,
				Lease:          event.Kv.Lease,
			}
		case clientv3.EventTypeDelete:
			delete(state.Keys, key)
		default:
			return nil, fmt.Errorf("unsupported etcd Watch event type %q", eventType)
		}
		if event.Kv.ModRevision > maxRevision {
			maxRevision = event.Kv.ModRevision
		}
		records = append(records, recoveryRecord{
			Type:          "watch_event",
			RunID:         runID,
			Timestamp:     now,
			EventRevision: event.Kv.ModRevision,
			EventType:     eventType,
			Key:           key,
			Value:         value,
		})
	}
	state.LastRevision = maxRevision
	state.UpdatedAt = now
	return records, nil
}

// signalContext is kept local to the recovery command so the existing pilot
// commands retain their current signal-handling behavior.
func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
