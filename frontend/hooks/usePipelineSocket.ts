// lib/sockets/usePipelineSocket.ts
"use client";
import { useEffect, useMemo, useRef } from "react";
import { decode, encode } from "@msgpack/msgpack";
import config from "@/env.config";
import useWebSocketStore from "@/store/web-socket";
import toast from "react-hot-toast";
import { parseJsonDeep } from "@/helpers/pipeline";
import { useSessionStore } from "@/store/session";
import { useUIStore } from "@/store/ui";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineRunStore } from "@/store/pipeline-run";
import { useRecommendationEngineStore } from "@/store/recommendation-engine";
import { useDatasetStore } from "@/store/dataset";
import { useTooltipStore } from "@/store/tooltip";
import { useNotificationsStore } from "@/store/notifications";
import { useQueueStatusStore } from "@/store/queue-status";
import { useExtractionStore } from "@/store/extraction";
import { ws } from "@/helpers/ws-events";
import { randomUUID } from "@/helpers/uuid";
import type { NotificationKind } from "@/types/notifications";
import type {
  WsPayloadMap,
  ProgressPayload,
  SuggestionPayload,
  SuggestionsRevokePayload,
  PipelineRunInitialisedPayload,
  PipelineRunStartedPayload,
  PipelineRunCompletedPayload,
  PipelineRunFailedPayload,
  PipelineRunCancelledPayload,
  PipelineRunErrorPayload,
  PipelineNodeStartedPayload,
  PipelineNodeCompletedPayload,
  PipelineNodeFailedPayload,
  PipelineNodeSkippedPayload,
  PipelineNodeCancelledPayload,
  PipelineNodeTraceOutputPayload,
  PipelineRewrittenPayload,
  StateDatasetPayload,
  StatePipelinePayload,
  StateLastRunPayload,
  CheckReportPayload,
  CheckEventPayload,
  NotificationPayload,
  NotificationBatchPayload,
  QueueStatusPayload,
  QueueConcurrencyLimitPayload,
  RateLimitedPayload,
} from "@/types/ws-payloads";

// Reconnect parameters
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

type Handlers = {
  onQueries?: (queries: any[]) => void;
  onProgress?: (process: any) => void;
  onSuggestion?: (suggestion: any) => void;
};

export function usePipelineSocket(handlers: Handlers = {}) {
  const { setSocket, setConnectionStatus } = useWebSocketStore();

  const { userId, activeSessionId, setTasks, setObjectives, setEvals, setCurrentEvals } =
    useSessionStore();
  const { setDatasets, updateDataset } = useDatasetStore();
  const {
    setName,
    setAvatar,
    setQueries,
    setSelectedTask,
    setSelectedObjectives,
    setObjectiveStatus,
    setSelectedEval,
    setToggle,
  } = useUIStore();
  const { setRecommendedPipelines } = useRecommendationEngineStore();

  const {
    setPipelineHistory,
    setTempPipeline,
    addSuggestion,
    addProgressItem,
    setAdapters,
    setOperators,
    setOperatorParams,
    clearSuggestions,
    clearProgressItems,
    setPendingGroupUpdate,
  } = usePipelineStore();

  const {
    setPipelineRun,
    updateNodeRunState,
    setPipelineRunStatus,
    setPipelineRunMetrics,
    setCheckReport,
    clearCheckReport,
    clearRun,
  } = usePipelineRunStore();

  const { setTooltips, setOnboardingState, tourCompleted, startTour } = useTooltipStore();
  const { push: pushNotification, pushBatch: pushNotificationBatch } = useNotificationsStore();
  const { setQueueStatus, setConcurrencyLimit, clearQueue } = useQueueStatusStore();
  const {
    setExtractedPipeline,
    setExtractionMeta,
    setExtractionError,
    setIsExtracting,
    setRulesContent,
    setIsSuggestingRules,
    setRuleSuggestions,
    dismissRuleSuggestion,
    setCompatRegressionReport,
  } = useExtractionStore();

  // ── Fix 1: stabilise caller-provided callbacks ──────────────────────────
  // Keep a ref that always points to the latest handlers object.
  // The effect only re-runs when connectedKey changes, so without this the
  // closured handler functions would go stale on every parent re-render.
  const handlersRef = useRef<Handlers>(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  // ── Fix 2: idempotency guard against stream replay ───────────────────────
  // If the Redis cursor ({uid}:{session}:last) ever expires and resets to 0,
  // send_messages() will re-deliver the entire stream.  We track seen keys
  // so each logical event is only processed once per WebSocket lifetime.
  // Keys are content-derived (not Redis stream IDs, which aren't forwarded).
  const seenRef = useRef<Set<string>>(new Set());

  // ── Batching: RAF micro-queue ─────────────────────────────────────────────
  // Instead of calling Zustand setters on every onmessage event (one React
  // re-render per message), we collect incoming messages and flush them in a
  // requestAnimationFrame callback.  React 18 automatic batching then merges
  // all set() calls within the flush into a single re-render (≤60 per second).
  const messageQueueRef = useRef<any[]>([]);
  const rafRef = useRef<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const connectedKey = useMemo(
    () => (userId && activeSessionId ? `${userId}:${activeSessionId}` : null),
    [userId, activeSessionId],
  );

  useEffect(() => {
    if (!connectedKey) return;

    // Clear the seen-set whenever a new connection key is established so we
    // don't carry over keys from a previous session.
    seenRef.current.clear();

    // ── Reset session-scoped stores before hydrating from the new session ──
    // Without this, stale data from the previous session persists in the
    // global Zustand stores until the new WS connection delivers state/*
    // events.  Clearing up-front prevents cross-session data leakage.
    setDatasets([]);
    clearSuggestions();
    clearProgressItems();
    clearRun();

    // ── Reconnect state (closed over per effect invocation) ───────────────
    let intentionalClose = false; // set true only on cleanup / user disconnect
    let reconnectAttempts = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    // Read navigator.onLine safely in SSR environments.
    let isOnline = typeof navigator !== "undefined" ? navigator.onLine : true;

    // ── Event handler map ──────────────────────────────────────────────────
    // Each entry handles one server-sent event type.  Adding a new event
    // requires only a single new key/value pair here — no switch needed.
    // All closures capture the same store setters and refs as before.
    // MsgHandler: typed for known events, falls back to `any` for the rest.
    type MsgHandler = ((resp: any) => void);
    const eventHandlers: Record<string, MsgHandler> = {
      // ── Session-scoped progress ──────────────────────────────────────────
      progress: (resp: ProgressPayload) => {
        if (resp.session !== activeSessionId) return;
        const metricName = resp.metafeature ?? resp.metric ?? "unknown";
        const pid = `${resp.uid}-${resp.session}-${metricName}-${resp.did}`;
        const item = { ...resp, pid } as any;
        item.metafeature = metricName;
        item.value = parseJsonDeep(resp.value);
        // Backend puts error text in `value` for MetafeatureError events
        if (resp.status === "error" && resp.value && resp.value !== "None") {
          item.error = String(resp.value);
          item.value = undefined;
        }
        addProgressItem(item);
        handlersRef.current.onProgress?.(resp);
      },

      // ── Suggestion (AI Debugger risk chain) ────────────────────────────
      suggestion: (resp: SuggestionPayload) => {
        // Redis stream fields arrive as strings — parse JSON arrays
        const suggestion = {
          ...resp,
          principles: parseJsonDeep(resp.principles) ?? [],
          checks: parseJsonDeep(resp.checks) ?? [],
          alternatives: parseJsonDeep(resp.alternatives) ?? [],
          has_rewrite: resp.has_rewrite === "true",
        };
        addSuggestion(suggestion as any);
        handlersRef.current.onSuggestion?.(suggestion);
      },

      // ── Suggestion scoping (AI Debugger applicability gating) ─────────
      "suggestions/revoke": (resp: SuggestionsRevokePayload) => {
        // Backend revokes suggestions for a removed operator.
        const operator = resp.operator;
        if (operator) {
          const { suggestions, setSuggestions } = usePipelineStore.getState();
          setSuggestions(suggestions.filter((s) => s.task !== operator));
        }
      },

      "suggestions/reset": () => {
        // Backend resets all suggestions (new pipeline or full scope change).
        usePipelineStore.getState().clearSuggestions();
      },

      // ── Mitigation applied (AI Debugger HITL feedback) ────────────────
      "ui/mitigation-applied": ({ value }) => {
        const mitigation = parseJsonDeep(value);
        if (mitigation) {
          toast.success(
            `${mitigation.mitigation}: ${mitigation.instruction ?? mitigation.risk}`,
            {
              duration: 3000,
              position: "top-center",
              style: { marginTop: "42vh" },
            },
          );
        }
      },

      // ── Mitigation failed ─────────────────────────────────────────────
      "ui/mitigation-failed": ({ value }) => {
        const data = parseJsonDeep(value);
        if (data) {
          toast.error(
            `${data.mitigation ?? "Mitigation"} could not be applied: ${data.reason ?? "unknown error"}`,
            { duration: 8000 },
          );
        }
      },

      // ── State hydration ─────────────────────────────────────────────────
      "state/dataset": ({ value }: StateDatasetPayload) => {
        const datasetMeta = parseJsonDeep(value);
        if (datasetMeta?.fpath) {
          const parts = String(datasetMeta.fpath).replace(/\\/g, "/").split("/");
          const filename = parts[parts.length - 1] ?? datasetMeta.fpath;
          setDatasets([{
            uuid: datasetMeta.did,
            did: datasetMeta.did,
            filename,
            size: 0,
            hasLabels: false,
            profile: datasetMeta.profile ?? undefined,
            quality: datasetMeta.quality ?? undefined,
            quality_checks: datasetMeta.quality_checks ?? undefined,
            quality_inputs: datasetMeta.quality_inputs ?? undefined,
            mitigation_session: datasetMeta.mitigation_session ?? undefined,
            target: datasetMeta.target ?? undefined,
            columns: datasetMeta.columns ?? undefined,
            features: datasetMeta.features ?? undefined,
          }]);
        } else {
          setDatasets([]);
        }
      },

      "state/quality": ({ did, value }) => {
        const quality = parseJsonDeep(value);
        if (did && quality && typeof quality === "object") {
          updateDataset(did, "quality", quality);
        }
      },

      "state/quality-checks": ({ did, value }) => {
        const checks = parseJsonDeep(value);
        if (did && checks && typeof checks === "object") {
          updateDataset(did, "quality_checks", checks);
        }
      },

      "state/column-profiles": ({ did, value }) => {
        const profiles = parseJsonDeep(value);
        if (did && profiles && typeof profiles === "object") {
          updateDataset(did, "columnProfiles", profiles);
        }
      },

      "state/data-mitigation-session": ({ did, value }) => {
        const mitigationSession = parseJsonDeep(value);
        if (did) {
          updateDataset(did, "mitigation_session", mitigationSession ?? undefined);
        }
      },

      "state/target": ({ value }) => {
        const target = parseJsonDeep(value);
        if (target) {
          console.debug("[WS] target restored:", target);
        }
      },

      // ── Restore selected data-science task on reconnect ──────────────
      "state/selected-task": ({ value }) => {
        // Empty value = backend says "no task selected for this session";
        // clear any leftover in-memory selection from a prior session
        // (Zustand store is not persisted but survives a session switch
        // in the same tab).
        if (!value) {
          setSelectedTask(undefined);
          return;
        }
        // Backend may send a plain name string (legacy) or a JSON blob
        // ``{name, auto, reason}`` when the task was auto-detected from
        // the dataset profile. Parse best-effort and fall back to string.
        try {
          const parsed =
            typeof value === "string" && value.trim().startsWith("{")
              ? JSON.parse(value)
              : null;
          if (parsed && typeof parsed === "object" && parsed.name) {
            setSelectedTask(parsed);
            return;
          }
        } catch {
          // fall through to string handling
        }
        setSelectedTask(value);
      },

      // ── Restore selected evaluation procedure on reconnect ──────────
      // Same idempotent contract as state/selected-task — empty value
      // explicitly clears any stale selection from a prior session.
      "state/selected-eval": ({ value }) => {
        setSelectedEval(value || undefined);
      },

      "state/lastRun": ({ value }: StateLastRunPayload) => {
        const lastRun = parseJsonDeep(value);
        if (lastRun?.metrics && typeof lastRun.metrics === "object") {
          const runId = lastRun.run_id ?? "restored";
          setPipelineRun({
            run_id: runId,
            status: lastRun.status === "FAILED" ? "failed" : "success",
            node_states: {},
            metrics: lastRun.metrics,
          });
        }
      },

      "state/pipeline": ({ value }: StatePipelinePayload) => {
        const pipelineHistory = typeof value === "string" ? JSON.parse(value) : value;
        if (pipelineHistory?.pipelines) {
          const processed = {
            ...pipelineHistory,
            pipelines: (pipelineHistory.pipelines as any[]).map((p) => {
              const pl = typeof p.pipeline === "string" ? JSON.parse(p.pipeline) : p.pipeline;
              return { ...p, ...pl };
            }),
          };
          setPipelineHistory(processed);
          const head = processed.pipelines.find((p: any) => p.id === processed.headId);
          if (head) {
            setTempPipeline({
              uuid: processed.uuid,
              nodes: head.nodes ?? [],
              edges: head.edges ?? [],
            } as any);
          }
        }
      },

      "user/name": ({ value }) => {
        setName(value);
        toast.success(`Welcome to Dorian, ${value}`);
      },

      "user/avatar": ({ value }) => setAvatar(value),

      "state/tasks": ({ value }) => {
        setTasks(
          value.map((raw: string) => {
            const parts = raw.split(":");
            return {
              uuid:        parts[0],
              name:        parts[1],
              description: parts[2] ?? "",
              date:        parts[3] ?? "",
              task:        parts[4] ?? "",
            };
          }),
        );
      },

      "state/operators": ({ value }) => {
        const parsed = (value as string[]).map((item) => {
          const [uuid, name, type] = item.split(":");
          return { uuid, name, ...(type ? { type } : {}) };
        });
        parsed.push({ uuid: "custom", name: "Custom Operator", type: "visualizer" } as any);
        setOperators(parsed);
      },

      "state/operator-params": ({ value }) => {
        const catalog = parseJsonDeep(value);
        if (catalog && typeof catalog === "object") {
          setOperatorParams(catalog);
        }
      },

      // ── Vault — missing env var check (cross-user pipeline reuse) ────
      "vault/check-required": (resp) => {
        const missing = parseJsonDeep(resp.missing) ?? [];
        if (Array.isArray(missing) && missing.length > 0) {
          try {
            // Lazy-require so missing store module doesn't break the WS loop.
            const vaultMod = require("@/store/vault");
            const setMissingVars = vaultMod?.useVaultStore?.getState?.()?.setMissingVars;
            if (typeof setMissingVars === "function") {
              setMissingVars(missing);
            }
          } catch {
            // Vault store not installed — silently drop, not fatal.
          }
        }
      },

      "state/adapters": ({ value }) => setAdapters(value),

      "state/objectives": ({ value }) => {
        const objectives = (value as string[]).map((item) => {
          const [uuid, name] = item.split(":");
          return { uuid, name };
        });
        console.debug("Setting objectives with value:", objectives);
        setObjectives(objectives);
      },

      "state/pipelines/recommendation": ({ value }) => {
        const recommendations = parseJsonDeep(value);
        console.debug("Setting recommendations with value:", recommendations);
        setRecommendedPipelines(recommendations);
      },

      // Root cause: backend sends type:"list" which the WS send loop
      // splits by comma.  Empty list → [""] after split, producing
      // {uuid:"", name:undefined} which crashes downstream renders.
      "state/evals": ({ value }) => {
        const items = Array.isArray(value) ? value : [];
        setEvals(
          items
            .filter((item) => typeof item === "string" && item.includes(":"))
            .map((item) => {
              const [uuid, ...rest] = item.split(":");
              return { uuid, name: rest.join(":") };
            }),
        );
      },

      // Restore custom evaluation procedures on reconnect
      "state/custom-evals": ({ value }) => {
        const customs = parseJsonDeep(value);
        if (Array.isArray(customs)) {
          setCurrentEvals(
            (customs.filter(
              (c: unknown): c is Record<string, unknown> =>
                typeof c === "object" && c !== null,
            ) as Record<string, unknown>[]).map((c) => ({
              uuid: String(c.uuid ?? c.id ?? ""),
              name: String(c.name ?? ""),
            })),
          );
        }
      },

      "state/queries": ({ value }) => {
        const queries = parseJsonDeep(value);
        setQueries(queries);
        handlersRef.current.onQueries?.(queries);
      },

      // Root cause: no backend emitter exists for "state/query" (dead handler).
      // Original code did value["task"]["name"] without null checks, which
      // crashes with TypeError if task/eval objects are missing from value.
      "state/query": ({ value }) => {
        if (!value || typeof value !== "object") return;
        const t = value["toggles"] ?? {};
        if (value["datasets"]) setDatasets(value["datasets"]);
        setToggle("DatasetUpload",      !!(t["dataset"]?.["add"]));
        setToggle("DatasetDelete",      !!(t["dataset"]?.["delete"]));
        setToggle("TaskSelection",      !!(t["task"]?.["select"]));
        setToggle("EvalSelection",      !!(t["eval"]?.["select"]));
        setToggle("ObjectiveSelection", !!(t["objectives"]?.["select"]));
        setToggle("ObjectiveDelete",    !!(t["objectives"]?.["select"]));
        setToggle("ObjectiveDragging",  !!(t["objectives"]?.["drag"]));
        if (value["task"]?.["name"]) setSelectedTask(value["task"]["name"]);
        if (value["eval"]?.["name"]) setSelectedEval(value["eval"]["name"]);
        if (value["objectives"]) setObjectives(value["objectives"]);
      },

      "state/objectives/selected": ({ value }) => {
        const items = Array.isArray(value) ? value : [];
        setSelectedObjectives(
          items
            .filter((item) => typeof item === "string" && item.includes(":"))
            .map((item) => {
              const [uuid, ...rest] = item.split(":");
              return { uuid, name: rest.join(":") };
            }),
        );
      },

      // ── Objective conflict reconciliation ─────────────────────────────────
      // Rust ``recommendation::handle_pipeline_objectives_switch`` emits this
      // when a pipeline lands in a session whose ranking objectives the user
      // has already customised (objectiveMode=custom). The dialog mounted at
      // the layout root reads ``objectivesConflict`` from useUIStore and lets
      // the user compose any subset of (shared ∪ current_only ∪ suggested_only)
      // — final list goes back as a normal RankingObjectivesChanged emit.
      "state/objectives/conflict-prompt": ({ value }) => {
        const conflict = parseJsonDeep(value);
        if (
          conflict &&
          typeof conflict === "object" &&
          Array.isArray(conflict.shared) &&
          Array.isArray(conflict.current_only) &&
          Array.isArray(conflict.suggested_only)
        ) {
          useUIStore.getState().setObjectivesConflict({
            current:        conflict.current        ?? [],
            suggested:      conflict.suggested      ?? [],
            shared:         conflict.shared         ?? [],
            current_only:   conflict.current_only   ?? [],
            suggested_only: conflict.suggested_only ?? [],
            trigger:        conflict.trigger        ?? "",
          });
        }
      },

      // ── Objective status (active / degraded) ──────────────────────────────
      "state/objectives/status": ({ value }) => {
        const status = parseJsonDeep(value);
        if (Array.isArray(status)) {
          setObjectiveStatus(status);
        }
      },

      // ── Objective validation (compile check feedback) ─────────────────────
      "state/objectives/validation": ({ value }) => {
        const result = parseJsonDeep(value);
        if (result && typeof result === "object") {
          if (!result.valid) {
            toast.error(`Objective "${result.name}": ${result.error}`);
          }
        }
      },

      // ── Pipeline execution events ────────────────────────────────────────

      // Strip internal prefixes so node states map back to canvas node IDs.
      // e.g. "printout_abc123" → "abc123"
      // ── Pipeline execution events ────────────────────────────────────────
      "pipeline/run/initialised": (resp: PipelineRunInitialisedPayload) => {
        setPipelineRun({ run_id: resp.run_id, status: "pending", node_states: {} });
        toast(`Pipeline queued`, { icon: "🚀" });
      },

      "pipeline/run/started": (resp: PipelineRunStartedPayload) => {
        setPipelineRunStatus(resp.run_id, "running");
        clearQueue(); // Pipeline is no longer queued — clear status bar
      },

      "pipeline/node/started": (resp: PipelineNodeStartedPayload) => {
        const id = resp.node_id.startsWith("printout_") ? resp.node_id.slice("printout_".length) : resp.node_id;
        updateNodeRunState(resp.run_id, id, {
          status: "running",
          start_time: resp.start_time ? parseFloat(resp.start_time) : undefined,
        });
      },

      "pipeline/node/completed": (resp: PipelineNodeCompletedPayload) => {
        const id = resp.node_id.startsWith("printout_") ? resp.node_id.slice("printout_".length) : resp.node_id;
        const state: { status: "success"; duration?: number; output?: unknown } = {
          status: "success",
          duration: resp.duration ? parseFloat(resp.duration) : undefined,
        };
        // Inline output from printout/visualizer nodes
        if (resp.output) {
          try {
            state.output = JSON.parse(resp.output);
          } catch {
            state.output = resp.output;
          }
        }
        updateNodeRunState(resp.run_id, id, state);
      },

      "pipeline/node/failed": (resp: PipelineNodeFailedPayload) => {
        const id = resp.node_id.startsWith("printout_") ? resp.node_id.slice("printout_".length) : resp.node_id;
        updateNodeRunState(resp.run_id, id, {
          status: "failed",
          error: resp.error,
          trace: resp.trace,
          duration: resp.duration ? parseFloat(resp.duration) : undefined,
        });
      },

      "pipeline/node/skipped": (resp: PipelineNodeSkippedPayload) => {
        const id = resp.node_id.startsWith("printout_") ? resp.node_id.slice("printout_".length) : resp.node_id;
        updateNodeRunState(resp.run_id, id, { status: "skipped" });
      },

      // Model tracing: trace output attributed to the parent operator node
      "pipeline/node/trace-output": (resp: PipelineNodeTraceOutputPayload) => {
        if (!resp.output) return;
        let output: unknown;
        try { output = JSON.parse(resp.output); } catch { output = resp.output; }
        updateNodeRunState(resp.run_id, resp.node_id, { output });
      },

      "pipeline/run/completed": (resp: PipelineRunCompletedPayload) => {
        setPipelineRunStatus(resp.run_id, "success");
        const metrics = resp.metrics
          ? typeof resp.metrics === "string"
            ? parseJsonDeep(resp.metrics)
            : resp.metrics
          : undefined;
        if (metrics && typeof metrics === "object" && Object.keys(metrics).length) {
          setPipelineRunMetrics(resp.run_id, metrics);
          toast.success("Pipeline completed — evaluation results available");
        } else {
          toast.success("Pipeline completed successfully");
        }
      },

      "pipeline/run/failed": (resp: PipelineRunFailedPayload) => {
        const errMsg = resp.error ?? resp.status ?? "unknown error";
        setPipelineRunStatus(resp.run_id, "failed", errMsg);
        toast.error(`Pipeline failed: ${errMsg}`);
      },

      "pipeline/run/cancelled": (resp: PipelineRunCancelledPayload) => {
        setPipelineRunStatus(resp.run_id, "cancelled");
        toast("Pipeline cancelled", { icon: "🛑" });
      },

      "pipeline/node/cancelled": (resp: PipelineNodeCancelledPayload) =>
        updateNodeRunState(resp.run_id, resp.node_id, { status: "cancelled" }),

      "pipeline/run/error": (resp: PipelineRunErrorPayload) =>
        toast.error(`Could not start pipeline: ${resp.reason ?? "unknown error"}`),

      // ── UI guidance (backend-authoritative tooltip content) ──────────────
      "ui/tooltips": ({ value }) => {
        const tooltipMap = parseJsonDeep(value);
        if (tooltipMap && typeof tooltipMap === "object") {
          setTooltips(tooltipMap);
        }
      },

      "ui/onboarding": ({ value }) => {
        const state = parseJsonDeep(value);
        if (state && typeof state === "object") {
          setOnboardingState(state as { tour_completed: boolean; tooltip_votes: Record<string, any> });
          // Auto-start tour ONLY for first-time users who have never seen it.
          // Backend tracks tour_completed per user in the docstore; we also set a
          // localStorage flag so the tour doesn't re-trigger on fast reconnects
          // before the backend round-trip completes.
          const LS_KEY = "dorian:tour_shown";
          const alreadyShownLocally = typeof window !== "undefined" && localStorage.getItem(LS_KEY) === "1";
          if (!state.tour_completed && !alreadyShownLocally) {
            if (typeof window !== "undefined") localStorage.setItem(LS_KEY, "1");
            setTimeout(() => {
              const s = useTooltipStore.getState();
              if (s.tooltips && !s.tourActive && !s.tourCompleted) {
                s.startTour();
              }
            }, 1500);
          }
        }
      },

      // ── Data check progress events (AI Debugger) ──────────────────────────
      "check/started": (resp: CheckEventPayload) => {
        console.debug("[WS] check started:", resp.check, "for", resp.risk, "on", resp.pipeline_label);
      },

      "check/passed": (resp: CheckEventPayload) => {
        console.debug("[WS] check passed:", resp.check, "for", resp.risk);
      },

      "check/failed": (resp: CheckEventPayload) => {
        console.debug("[WS] check failed:", resp.check, "for", resp.risk, "—", resp.message);
      },

      "check/report": (resp: CheckReportPayload) => {
        const results = parseJsonDeep(resp.results) ?? [];
        setCheckReport({
          pipelineLabel: resp.pipeline_label ?? "",
          total: parseInt(resp.total ?? "0", 10) || 0,
          passed: parseInt(resp.passed ?? "0", 10) || 0,
          failed: parseInt(resp.failed ?? "0", 10) || 0,
          skipped: parseInt(resp.skipped ?? "0", 10) || 0,
          results: Array.isArray(results) ? results : [],
        });
      },

      // ── In-app notification (bell center) ────────────────────────────────
      notification: (resp: NotificationPayload) => {
        pushNotification({
          id: resp.id,
          kind: resp.kind as NotificationKind,
          title: resp.title,
          message: resp.message,
          createdAt: resp.createdAt ? parseInt(resp.createdAt, 10) : Date.now(),
        });
      },

      // ── Batch notifications (reconnect replay) ─────────────────────────
      "notifications/batch": (resp: NotificationBatchPayload) => {
        const items = typeof resp.value === "string" ? JSON.parse(resp.value) : resp.value;
        if (Array.isArray(items)) {
          pushNotificationBatch(
            items.map((n) => ({
              id: n.id ?? randomUUID(),
              kind: (n.kind ?? "info") as NotificationKind,
              title: n.title ?? "",
              message: n.message,
              createdAt: n.createdAt ? parseInt(String(n.createdAt), 10) : Date.now(),
              read: false,
              meta: n.meta as Record<string, unknown> | undefined,
            })),
          );
          if (items.length > 0) {
            toast(`${items.length} notification${items.length > 1 ? "s" : ""} while you were away`, {
              icon: "🔔",
              duration: 4000,
            });
          }
        }
      },

      // ── Queue status (position + ETA updates) ──────────────────────────
      "queue/status": (resp: QueueStatusPayload) => {
        const status = typeof resp.value === "string" ? JSON.parse(resp.value) : resp.value;
        if (status && typeof status === "object") {
          setQueueStatus(status);
        }
      },

      // ── Queue concurrency limit reached ────────────────────────────────
      "queue/concurrency-limit": (resp: QueueConcurrencyLimitPayload) => {
        const limit = typeof resp.value === "string" ? JSON.parse(resp.value) : resp.value;
        if (limit && typeof limit === "object") {
          setConcurrencyLimit(limit);
          toast(
            `You're running ${limit.current}/${limit.max} concurrent pipelines (${limit.tier_label} tier)`,
            { icon: "⏳", duration: 5000 },
          );
        }
      },

      // ── Group / Node created (backend response to operator DnD) ──────────
      "state/group-created": (resp) => {
        const groupData = parseJsonDeep(resp.value);
        const nodeId = resp.nodeId;
        if (!groupData || !nodeId) return;

        // Signal the canvas to update the placeholder group node with
        // the backend-built sub-DAG data (children, ioMap, internal edges).
        setPendingGroupUpdate({ nodeId, data: groupData });
      },

      "state/node-created": (resp) => {
        // Simple operator — backend confirmed it's a function-interface
        // operator (no sub-DAG). The node already exists on canvas from DnD,
        // so this is a no-op acknowledgement (log only).
        console.debug("[WS] node-created ack:", resp.nodeId);
      },

      // ── Pipeline rewrite (mitigation accepted + KB-driven rewrite) ────────
      "pipeline/rewritten": (resp: PipelineRewrittenPayload) => {
        const pipeline = typeof resp.pipeline === "string" ? JSON.parse(resp.pipeline) : resp.pipeline;
        if (pipeline) {
          // Update the canvas immediately with the rewritten pipeline so
          // the user sees the new parameter / operator change.
          setTempPipeline(pipeline);
          toast.success(resp.summary ?? "Pipeline rewritten", { duration: 5000 });
        }
      },

      // ── Pipeline extraction WS responses ──────────────────────────────────
      "extraction/result": ({ value }) => {
        const result = parseJsonDeep(value);
        // Always clear the spinner regardless of payload shape
        setIsExtracting(false);
        if (result && typeof result === "object") {
          setExtractedPipeline(result);
          if (result.extractionId && result.rulesVersion) {
            setExtractionMeta(result.extractionId, result.rulesVersion);
          }
        }
      },

      "extraction/error": ({ value }) => {
        const err = parseJsonDeep(value);
        if (err && typeof err === "object") {
          setExtractionError(err.error ?? "Extraction failed", err.trace ?? null);
        } else {
          setExtractionError("Extraction failed", null);
        }
        setIsExtracting(false);
      },

      "extraction/rules": ({ value }) => {
        const rules = parseJsonDeep(value);
        if (rules && typeof rules === "object") {
          const fmt = rules.format === "json_specs" ? "json_specs" : "python_rules";
          setRulesContent(
            rules.content ?? "",
            rules.source === "user" ? "user" : "default",
            fmt,
          );
        }
      },

      "extraction/rules-saved": ({ value }) => {
        const status = parseJsonDeep(value);
        if (!status || typeof status !== "object") return;
        if (status.status === "ok") {
          toast.success(
            status.compatOverride
              ? "Extraction rules saved (backward-compat override recorded)"
              : "Extraction rules saved",
          );
        } else if (status.status === "invalid") {
          toast.error(status.error ?? "Rules are invalid — saved as invalid version");
        } else if (status.status === "error") {
          toast.error(status.error ?? "Failed to save extraction rules");
        }
      },

      "extraction/rules-compat-regressions": ({ value }) => {
        // Save blocked because candidate rules regress N past extractions.
        // The store holds the report so the ExtractionView can render a
        // diff modal offering: (a) abandon, (b) edit rules, (c) override
        // and retry with skipCompatCheck=true.
        const report = parseJsonDeep(value);
        if (report && typeof report === "object") {
          setCompatRegressionReport(report);
        }
      },

      // ── LLM rule suggestions ──────────────────────────────────────────────
      "extraction/rules-suggestion": ({ value }) => {
        const data = parseJsonDeep(value);
        if (data && typeof data === "object" && Array.isArray(data.rules)) {
          setRuleSuggestions(data.suggestionId ?? "", data.rules);
        } else {
          setIsSuggestingRules(false);
        }
      },

      "extraction/suggest-error": ({ value }) => {
        const err = parseJsonDeep(value);
        setIsSuggestingRules(false);
        toast.error(
          typeof err === "object" && err?.error
            ? `Rule suggestion failed: ${err.error}`
            : "Rule suggestion failed",
        );
      },

      "extraction/rule-accepted": ({ value }) => {
        const data = parseJsonDeep(value);
        if (!data || typeof data !== "object") return;
        if (data.error) {
          toast.error(`Could not accept rule: ${data.error}`);
          return;
        }
        dismissRuleSuggestion(data.ruleId);
        if (data.newRulesContent) {
          useExtractionStore.getState().setRulesContent(data.newRulesContent, "user");
        }
        toast.success(
          data.isPartial
            ? "Partial rule applied — request another suggestion to continue closing the gap"
            : "Rule added to your extraction rules",
        );
      },

      "extraction/rule-rejected": ({ value }) => {
        const data = parseJsonDeep(value);
        if (data?.ruleId) dismissRuleSuggestion(data.ruleId);
      },

      "mcp/token-issued": ({ value }) => {
        // Re-dispatch as a DOM event — the McpConnectDialog listens for
        // this rather than wiring through the extraction store, because
        // the token is session-local and never needs to persist.
        const data = parseJsonDeep(value);
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("mcp-token-issued", { detail: data }));
        }
      },

      "extraction/rules-updated": ({ value }) => {
        // Fired by the MCP persist tool. Re-issue the load to refresh
        // the card UI with the new rules list.
        const data = parseJsonDeep(value);
        if (data && typeof data === "object") {
          toast.success(
            `Rules updated via MCP (${data.count} total${
              typeof data.insert_at === "number" ? `, inserted at ${data.insert_at}` : ""
            })`,
          );
          ws.loadExtractionRules({});
        }
      },

      "extraction/partial-applied": ({ value }) => {
        // Partial rule was committed; backend updated the extraction's
        // autoDag to the improved intermediate. Push the new DAG onto
        // the canvas so the user sees the progress without a reload.
        const data = parseJsonDeep(value);
        if (!data || typeof data !== "object" || !data.updatedDag) return;
        useExtractionStore.getState().setExtractedPipeline(data.updatedDag);
        if (typeof data.gedBefore === "number" && typeof data.gedAfter === "number") {
          toast.success(
            `Applied partial rule. GED ${data.gedBefore} → ${data.gedAfter}.`,
          );
        }
      },

      // ── Rate limiting ─────────────────────────────────────────────────────
      "error/rate-limited": (resp: RateLimitedPayload) => {
        const eventName = resp.eventName ?? "request";
        const retryAfter = Number(resp.retryAfter ?? 60);
        const limit = Number(resp.limit ?? 0);
        toast.error(
          `Too many ${eventName} requests (limit: ${limit}/min) — retry in ${retryAfter}s`,
          { duration: Math.min(retryAfter * 1000, 10_000) },
        );
      },
    };

    // ── processMessage ─────────────────────────────────────────────────────
    // Handles dedup, then dispatches to the handler map above.
    // Called from the RAF flush, not from onmessage directly.
    const processMessage = (resp: any) => {
      const { event } = resp;

      // ── Fix 2 (cont.): derive a dedup key ─────────────────────────────
      const dedupKey = resp.run_id
        ? `${event}:${resp.run_id}:${resp.node_id ?? ""}:${resp.status ?? ""}`
        : resp.request_id
          ? `req:${resp.request_id}`
          : null;

      if (dedupKey) {
        if (seenRef.current.has(dedupKey)) {
          console.debug("[WS] dedup drop", dedupKey);
          return;
        }
        seenRef.current.add(dedupKey);
        // Evict the oldest entry once the Set exceeds 1000 keys so it doesn't
        // grow unbounded over a long-lived WebSocket connection.
        if (seenRef.current.size > 1000) {
          seenRef.current.delete(seenRef.current.values().next().value as string);
        }
      }

      console.debug("[WS] message", { event, value: resp.value });

      const handler = eventHandlers[event];
      if (handler) {
        try {
          handler(resp);
        } catch (err) {
          console.error("[WS] Handler error for event:", event, err);
          toast.error(`Event handler error: ${event}`);
        }
      } else {
        console.debug("[WS] unhandled event", event);
      }
    }; // end processMessage

    // ── connect() ──────────────────────────────────────────────────────────
    // Creates a WebSocket and wires up all event handlers.
    // Safe to call multiple times — guards against duplicate sockets.
    const connect = () => {
      if (wsRef.current) return; // already open or connecting

      setConnectionStatus("connecting");

      const ws = new WebSocket(
        `${config.ws}?uid=${userId}&session=${activeSessionId}`,
      );
      // Receive binary frames from the backend (msgpack-encoded)
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      // ── onopen ───────────────────────────────────────────────────────────
      ws.onopen = () => {
        console.debug("[WS] open", ws.url);

        reconnectAttempts = 0;

        setConnectionStatus("connected");
        setSocket(ws);

        ws.send(
          encode({
            event: "init",
            user: userId,
            session: activeSessionId,
          }),
        );
      };

      // ── onerror ──────────────────────────────────────────────────────────
      // Fires before onclose; browsers provide no useful detail in the error
      // event, and ``onclose`` will log + retry. Suppress the redundant
      // warn so a backend-down state doesn't flood the console with
      // identical "connection error" lines on every retry tick. Real
      // diagnostics still go through ``onclose`` (status code, reason,
      // backoff schedule) once per attempt.
      ws.onerror = () => {
        setConnectionStatus("error");
      };

      // ── onclose ──────────────────────────────────────────────────────────
      ws.onclose = (ev) => {
        // Code 1006 (abnormal closure, no close handshake) is the
        // norm — server restart, page navigation, suspended laptop,
        // backend redeploy. The reconnect logic below handles them
        // all silently; logging would just spam the console. Real
        // close codes (1001 going away, 1008 policy violation,
        // 1011 server error) still log at warn so they show up in
        // browser logs.
        const isExpectedClose = ev.code === 1006 || ev.code === 1000;
        if (reconnectAttempts === 0 && !isExpectedClose) {
          console.warn("[WS] close", {
            code: ev.code,
            reason: ev.reason,
            wasClean: ev.wasClean,
          });
        }

        wsRef.current = null;
        setSocket(null);

        if (intentionalClose) return; // user navigated away or switched session

        if (!isOnline) {
          // Network is down — wait for the 'online' event instead of retrying.
          setConnectionStatus("offline");
          return;
        }

        // Backend rate-limiter rejection (HTTP code 1008 — Policy
        // Violation, used for "too many connections from this IP").
        // Retrying immediately just refills the counter and keeps us
        // locked out. Sleep long enough for the 60 s sliding window to
        // drain, then try once. Avoids the retry-storm → rate-limit-
        // lockout feedback loop that we hit after a backend restart.
        if (ev.code === 1008) {
          setConnectionStatus("reconnecting");
          console.warn(
            `[WS] server rate-limited this IP (1008: ${ev.reason || "policy"}). ` +
            `Cooling off for 65s before retrying.`,
          );
          reconnectAttempts = 0; // reset the exponential backoff counter
          reconnectTimer = setTimeout(connect, 65_000);
          return;
        }

        // Exponential backoff: 1 s → 2 s → 4 s … capped at 30 s.
        const delay = Math.min(
          RECONNECT_BASE_MS * Math.pow(2, reconnectAttempts),
          RECONNECT_MAX_MS,
        );
        reconnectAttempts += 1;

        setConnectionStatus("reconnecting");

        console.debug(
          `[WS] reconnecting in ${delay} ms (attempt ${reconnectAttempts})`,
        );
        reconnectTimer = setTimeout(connect, delay);
      };

      // ── onmessage ────────────────────────────────────────────────────────
      ws.onmessage = (ev) => {
        let resp: any;
        try {
          // Backend sends msgpack-encoded binary frames
          resp = decode(new Uint8Array(ev.data as ArrayBuffer));
        } catch {
          return;
        }

        // Push to queue; schedule a single RAF flush if not already pending.
        messageQueueRef.current.push(resp);
        if (rafRef.current === null) {
          rafRef.current = requestAnimationFrame(() => {
            rafRef.current = null;
            // Drain the queue — React 18 batches all set() calls within one
            // synchronous callback into a single re-render.
            const batch = messageQueueRef.current.splice(0);
            for (const msg of batch) {
              processMessage(msg);
            }
          });
        }
      };
    }; // end connect()

    // ── Window online / offline ───────────────────────────────────────────
    const handleOffline = () => {
      isOnline = false;
      setConnectionStatus("offline");
      console.debug("[WS] browser went offline");
    };

    const handleOnline = () => {
      isOnline = true;
      console.debug("[WS] browser came back online — reconnecting");
      // Cancel any pending backoff timer and reconnect immediately.
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      reconnectAttempts = 0;
      if (!wsRef.current) connect();
    };

    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);

    // Kick off the initial connection.
    connect();

    // ── Cleanup ───────────────────────────────────────────────────────────
    return () => {
      intentionalClose = true;

      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      messageQueueRef.current = [];

      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);

      wsRef.current?.close();
      wsRef.current = null;
      setSocket(null);
      setConnectionStatus("idle");
    };
  }, [connectedKey]); // eslint-disable-line react-hooks/exhaustive-deps
}
