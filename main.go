package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	clientv3 "go.etcd.io/etcd/client/v3"
)

const defaultEndpoint = "http://127.0.0.1:2379"

var validRunID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

type producerRecord struct {
	Type           string  `json:"type"`
	RunID          string  `json:"run_id"`
	Sequence       int     `json:"sequence"`
	RequestID      string  `json:"request_id"`
	Key            string  `json:"key"`
	Revision       int64   `json:"revision"`
	StartedAt      string  `json:"started_at"`
	AcknowledgedAt string  `json:"acknowledged_at"`
	LatencyMs      float64 `json:"latency_ms"`
	ValueSHA256    string  `json:"value_sha256"`
}

type watchRecord struct {
	Type            string `json:"type"`
	RunID           string `json:"run_id"`
	ReceivedAt      string `json:"received_at"`
	HeaderRevision  int64  `json:"header_revision,omitempty"`
	EventRevision   int64  `json:"event_revision,omitempty"`
	EventType       string `json:"event_type,omitempty"`
	Key             string `json:"key,omitempty"`
	Value           string `json:"value,omitempty"`
	CompactRevision int64  `json:"compact_revision,omitempty"`
	Canceled        bool   `json:"canceled,omitempty"`
	Error           string `json:"error,omitempty"`
}

type payload struct {
	RunID     string `json:"run_id"`
	Sequence  int    `json:"sequence"`
	RequestID string `json:"request_id"`
	CreatedAt string `json:"created_at"`
	Message   string `json:"message"`
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "watchlab:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		printUsage(os.Stderr)
		return errors.New("expected command: produce, watch or recover")
	}

	switch args[0] {
	case "produce":
		return runProduce(args[1:])
	case "watch":
		return runWatch(args[1:])
	case "recover":
		return runRecover(args[1:])
	case "help", "-h", "--help":
		printUsage(os.Stdout)
		return nil
	default:
		printUsage(os.Stderr)
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, `WatchLab pilot

Usage:
  watchlab produce [flags]
  watchlab watch   [flags]
  watchlab recover [flags]

Commands:
  produce  Write a sequence of test events into etcd and record acknowledged revisions.
  watch    Subscribe to an event prefix and record Watch responses as JSONL.
  recover  Rebuild local state with Range and continue with Watch; resnapshot after compaction.

Use "watchlab <command> -h" to see command flags.`)
}

func runProduce(args []string) error {
	fs := flag.NewFlagSet("produce", flag.ContinueOnError)
	endpoint := fs.String("endpoint", defaultEndpoint, "etcd client endpoint")
	runID := fs.String("run-id", "pilot-001", "experiment ID: letters, digits, underscore and hyphen")
	prefix := fs.String("prefix", "", "event key prefix; defaults to /watchlab/runs/<run-id>/events/")
	count := fs.Int("count", 10, "number of events to write")
	interval := fs.Duration("interval", 500*time.Millisecond, "delay between writes")
	out := fs.String("out", "", "JSONL output file; defaults to results/<run-id>-producer.jsonl")
	requestTimeout := fs.Duration("request-timeout", 5*time.Second, "timeout for each PUT")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *count < 1 || *count > 100000 {
		return errors.New("--count must be between 1 and 100000")
	}
	if *interval < 0 {
		return errors.New("--interval cannot be negative")
	}
	if *requestTimeout <= 0 {
		return errors.New("--request-timeout must be positive")
	}
	if err := validateRunID(*runID); err != nil {
		return err
	}
	if *prefix == "" {
		*prefix = defaultPrefix(*runID)
	}
	if !strings.HasSuffix(*prefix, "/") {
		return errors.New("--prefix must end with '/'")
	}
	if *out == "" {
		*out = filepath.Join("results", *runID+"-producer.jsonl")
	}

	file, encoder, err := createJSONL(*out)
	if err != nil {
		return err
	}
	defer file.Close()

	cli, err := newClient(*endpoint)
	if err != nil {
		return err
	}
	defer cli.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	fmt.Printf("PRODUCER_READY run_id=%s prefix=%s count=%d output=%s\n", *runID, *prefix, *count, *out)

	for seq := 1; seq <= *count; seq++ {
		select {
		case <-ctx.Done():
			fmt.Printf("PRODUCER_STOPPED completed=%d reason=%v\n", seq-1, ctx.Err())
			return nil
		default:
		}

		requestID := fmt.Sprintf("%s-%06d", *runID, seq)
		key := fmt.Sprintf("%s%06d", *prefix, seq)
		valueBytes, err := json.Marshal(payload{
			RunID:     *runID,
			Sequence:  seq,
			RequestID: requestID,
			CreatedAt: time.Now().UTC().Format(time.RFC3339Nano),
			Message:   fmt.Sprintf("watchlab test event %06d", seq),
		})
		if err != nil {
			return fmt.Errorf("marshal event %d: %w", seq, err)
		}

		started := time.Now()
		requestCtx, cancel := context.WithTimeout(ctx, *requestTimeout)
		response, putErr := cli.Put(requestCtx, key, string(valueBytes))
		cancel()
		if putErr != nil {
			return fmt.Errorf("PUT seq=%d key=%q: %w", seq, key, putErr)
		}
		if response.Header == nil {
			return fmt.Errorf("PUT seq=%d returned no response header", seq)
		}
		elapsed := time.Since(started)
		acknowledgedAt := time.Now().UTC()
		record := producerRecord{
			Type:           "put_ack",
			RunID:          *runID,
			Sequence:       seq,
			RequestID:      requestID,
			Key:            key,
			Revision:       response.Header.Revision,
			StartedAt:      started.UTC().Format(time.RFC3339Nano),
			AcknowledgedAt: acknowledgedAt.Format(time.RFC3339Nano),
			LatencyMs:      float64(elapsed.Nanoseconds()) / 1e6,
			ValueSHA256:    sha256Hex(valueBytes),
		}
		if err := encoder.Encode(record); err != nil {
			return fmt.Errorf("write producer record: %w", err)
		}
		fmt.Printf("PUT seq=%d revision=%d latency=%.3fms key=%s\n", seq, record.Revision, record.LatencyMs, key)

		if seq < *count && *interval > 0 {
			timer := time.NewTimer(*interval)
			select {
			case <-ctx.Done():
				timer.Stop()
				fmt.Printf("PRODUCER_STOPPED completed=%d reason=%v\n", seq, ctx.Err())
				return nil
			case <-timer.C:
			}
		}
	}

	fmt.Printf("PRODUCER_DONE count=%d output=%s\n", *count, *out)
	return nil
}

func runWatch(args []string) error {
	fs := flag.NewFlagSet("watch", flag.ContinueOnError)
	endpoint := fs.String("endpoint", defaultEndpoint, "etcd client endpoint")
	runID := fs.String("run-id", "pilot-001", "experiment ID")
	prefix := fs.String("prefix", "", "event key prefix; defaults to /watchlab/runs/<run-id>/events/")
	startRevision := fs.Int64("start-revision", 0, "inclusive starting revision; 0 means events after watch creation")
	out := fs.String("out", "", "JSONL output file; defaults to results/<run-id>-watcher.jsonl")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := validateRunID(*runID); err != nil {
		return err
	}
	if *startRevision < 0 {
		return errors.New("--start-revision cannot be negative")
	}
	if *prefix == "" {
		*prefix = defaultPrefix(*runID)
	}
	if !strings.HasSuffix(*prefix, "/") {
		return errors.New("--prefix must end with '/'")
	}
	if *out == "" {
		*out = filepath.Join("results", *runID+"-watcher.jsonl")
	}

	file, encoder, err := createJSONL(*out)
	if err != nil {
		return err
	}
	defer file.Close()

	cli, err := newClient(*endpoint)
	if err != nil {
		return err
	}
	defer cli.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	watchOptions := []clientv3.OpOption{
		clientv3.WithPrefix(),
		clientv3.WithCreatedNotify(),
	}
	if *startRevision > 0 {
		watchOptions = append(watchOptions, clientv3.WithRev(*startRevision))
	}

	fmt.Printf("WATCH_START run_id=%s prefix=%s start_revision=%d output=%s\n", *runID, *prefix, *startRevision, *out)
	watchCh := cli.Watch(ctx, *prefix, watchOptions...)
	created := false

	for {
		select {
		case <-ctx.Done():
			fmt.Printf("WATCH_STOPPED reason=%v\n", ctx.Err())
			return nil
		case response, ok := <-watchCh:
			if !ok {
				if ctx.Err() != nil {
					fmt.Printf("WATCH_STOPPED reason=%v\n", ctx.Err())
					return nil
				}
				return errors.New("watch channel closed unexpectedly")
			}

			headerRevision := int64(0)
			if response.Header != nil {
				headerRevision = response.Header.Revision
			}

			if response.Canceled {
				reason := "watch canceled without a reported error"
				if err := response.Err(); err != nil {
					reason = err.Error()
				}
				record := watchRecord{
					Type:            "watch_canceled",
					RunID:           *runID,
					ReceivedAt:      time.Now().UTC().Format(time.RFC3339Nano),
					HeaderRevision:  headerRevision,
					CompactRevision: response.CompactRevision,
					Canceled:        true,
					Error:           reason,
				}
				if err := encoder.Encode(record); err != nil {
					return fmt.Errorf("write canceled record: %w", err)
				}
				fmt.Printf("WATCH_CANCELED compact_revision=%d error=%s\n", response.CompactRevision, reason)
				return fmt.Errorf("watch canceled: %s", reason)
			}

			if err := response.Err(); err != nil {
				return fmt.Errorf("watch response: %w", err)
			}

			if response.Created {
				created = true
				record := watchRecord{
					Type:           "watch_created",
					RunID:          *runID,
					ReceivedAt:     time.Now().UTC().Format(time.RFC3339Nano),
					HeaderRevision: headerRevision,
				}
				if err := encoder.Encode(record); err != nil {
					return fmt.Errorf("write watch-created record: %w", err)
				}
				fmt.Printf("WATCH_CREATED header_revision=%d\n", headerRevision)
				continue
			}

			if len(response.Events) == 0 {
				continue
			}
			if !created {
				fmt.Fprintln(os.Stderr, "warning: received events before explicit created notification")
			}

			for _, event := range response.Events {
				if event == nil || event.Kv == nil {
					continue
				}
				record := watchRecord{
					Type:           "watch_event",
					RunID:          *runID,
					ReceivedAt:     time.Now().UTC().Format(time.RFC3339Nano),
					HeaderRevision: headerRevision,
					EventRevision:  event.Kv.ModRevision,
					EventType:      event.Type.String(),
					Key:            string(event.Kv.Key),
					Value:          string(event.Kv.Value),
				}
				if err := encoder.Encode(record); err != nil {
					return fmt.Errorf("write event record: %w", err)
				}
				fmt.Printf("EVENT type=%s event_revision=%d header_revision=%d key=%s\n", record.EventType, record.EventRevision, record.HeaderRevision, record.Key)
			}
		}
	}
}

func newClient(endpoint string) (*clientv3.Client, error) {
	return clientv3.New(clientv3.Config{
		Endpoints:   []string{endpoint},
		DialTimeout: 5 * time.Second,
	})
}

func createJSONL(path string) (*os.File, *json.Encoder, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, nil, fmt.Errorf("create output directory: %w", err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o644)
	if err != nil {
		return nil, nil, fmt.Errorf("create output file %q (it may already exist): %w", path, err)
	}
	return file, json.NewEncoder(file), nil
}

func validateRunID(runID string) error {
	if !validRunID.MatchString(runID) {
		return errors.New("run-id may contain only ASCII letters, digits, '_' and '-'")
	}
	return nil
}

func defaultPrefix(runID string) string {
	return "/watchlab/runs/" + runID + "/events/"
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
