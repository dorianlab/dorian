"use client";

import env from "@/env.config";
import { useAgentStore } from "@/store/agent";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Switch } from "@/components/ui/switch";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";

/* ────────────────────────────────────────────────────────────────────── */
/*  Agent Panel                                                          */
/* ────────────────────────────────────────────────────────────────────── */

export default function AgentPanel() {
  const { panelOpen, setPanelOpen, agentMode, setAgentMode } = useAgentStore();

  return (
    <Dialog open={panelOpen} onOpenChange={setPanelOpen}>
      <DialogContent className='sm:max-w-2xl max-h-[80vh]'>
        <DialogHeader>
          <DialogTitle className='text-lg font-semibold'>
            Agent Panel
          </DialogTitle>
          <DialogDescription>
            Configure agent integration and browse the API reference.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue='settings'>
          <TabsList className='w-full'>
            <TabsTrigger value='settings' className='flex-1'>
              Settings
            </TabsTrigger>
            <TabsTrigger value='reference' className='flex-1'>
              API Reference
            </TabsTrigger>
          </TabsList>

          {/* ── Settings ─────────────────────────────────── */}
          <TabsContent value='settings'>
            <div className='space-y-6 py-4'>
              <div className='flex items-center justify-between gap-4'>
                <div className='space-y-1'>
                  <Label htmlFor='agent-mode' className='text-sm font-medium'>
                    Agent Mode
                  </Label>
                  <p className='text-xs text-muted-foreground leading-relaxed'>
                    When enabled, every outbound WebSocket event includes{" "}
                    <code className='rounded bg-muted px-1 py-0.5 text-xs'>
                      agentDriven: true
                    </code>{" "}
                    so the backend can distinguish agent-driven actions from
                    human actions.
                  </p>
                </div>
                <div className='flex items-center gap-2 shrink-0'>
                  {agentMode && (
                    <Badge variant='default' className='text-xs'>
                      Active
                    </Badge>
                  )}
                  <Switch
                    id='agent-mode'
                    checked={agentMode}
                    onCheckedChange={setAgentMode}
                  />
                </div>
              </div>
              <Separator />
              <p className='text-xs text-muted-foreground'>
                Agent mode persists for the current browser session. Reloading
                the page resets it to off.
              </p>
            </div>
          </TabsContent>

          {/* ── API Reference ────────────────────────────── */}
          <TabsContent value='reference'>
            <ScrollArea className='h-[55vh] pr-4'>
              <AgentReference />
            </ScrollArea>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

/* ────────────────────────────────────────────────────────────────────── */
/*  Static API Reference (machine-readable)                              */
/* ────────────────────────────────────────────────────────────────────── */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className='space-y-2'>
      <h3 className='font-semibold text-sm'>{title}</h3>
      {children}
    </section>
  );
}

function SubSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className='text-xs font-medium text-muted-foreground mt-3 mb-1'>
        {title}
      </h4>
      {children}
    </div>
  );
}

function Pre({ children }: { children: string }) {
  return (
    <pre className='rounded bg-muted p-3 text-xs overflow-x-auto whitespace-pre leading-relaxed'>
      {children}
    </pre>
  );
}

function AgentReference() {
  return (
    <div className='space-y-6 py-2 text-sm'>
      {/* ── Connection ───────────────────────────────────── */}
      <Section title='Connection'>
        <Pre>{`WebSocket   ${env.ws}
Protocol    msgpack binary frames
Backend     ${env.backend}

All WS messages are msgpack-encoded.
REST endpoints accept/return JSON.`}</Pre>
      </Section>

      <Separator />

      {/* ── Safety & Rate Limits ─────────────────────────── */}
      <Section title='Safety Guidelines'>
        <p className='text-xs text-muted-foreground mb-2'>
          Agents share backend resources with human users. Follow these
          rules to avoid overwhelming the server.
        </p>
        <Pre>{`RULE                               DETAILS
─────────────────────────────────  ────────────────────────────────────
One WS connection per session      Do NOT open multiple sockets
Wait for state/* events            After InitSession, wait for the full
                                   seed (state/tasks, state/operators,
                                   state/evals, ...) before emitting
Min 200 ms between WS events       Batch rapid canvas edits; never fire
                                   node/edge events in a tight loop
One pipeline run at a time         Wait for pipeline/run/completed or
                                   pipeline/run/failed before starting
                                   the next execution
Respect queue/status               If you receive queue/status, back off
                                   until the pipeline is dequeued
Handle error/rate-limited          Exponential backoff (1s, 2s, 4s, max 30s)
Never poll REST in a loop          Use WS events as the notification
                                   channel; REST is for one-shot reads
Save before execute                Always emit PipelineSaved before
                                   ExecutePipeline
Cancel gracefully                  Emit CancelPipeline and wait for
                                   pipeline/run/cancelled before retrying
Clean up sessions                  DELETE /session/{id} when done; don't
                                   leave orphan sessions`}</Pre>
      </Section>

      <Separator />

      {/* ── Flow 1: Session + Dataset ────────────────────── */}
      <Section title='Flow 1: Create Session and Upload Dataset'>
        <p className='text-xs text-muted-foreground mb-2'>
          Every agent run starts by creating a session and uploading data.
          Wait for the full seed before proceeding.
        </p>
        <Pre>{`# 1a. Create session
POST /session/create  form: { uid, name }
  -> { session_id }

# 1b. Connect WebSocket (auto-sends InitSession)
WS connect: ${env.ws}?uid={uid}&session={session_id}
  -> wait for state/* seed events (tasks, operators, evals, objectives)

# 1c. Upload dataset
POST /upload  form: { file, session_id, user_id }
  -> WS receives: progress (repeated), state/dataset, state/queries
  -> wait for state/dataset before proceeding

# 1d. (Optional) Import existing dataset into session
POST /datasets/{did}/import  query: { did, session_id, user_id }
  -> WS receives: state/dataset`}</Pre>
        <SubSection title='Related REST endpoints'>
          <Pre>{`GET  /session/list         query: uid       -> list sessions
GET  /session/{id}                          -> session details
GET  /session/{id}/state                    -> full UI state (REST hydration)
POST /session/rename       form: session_id, new_title
DELETE /session/{id}        query: uid
GET  /datasets             query: uid       -> list available datasets
PATCH /datasets/{did}/visibility  query: did, user_id, is_public`}</Pre>
        </SubSection>
      </Section>

      <Separator />

      {/* ── Flow 2: Task + Recommendations ───────────────── */}
      <Section title='Flow 2: Select Task and Get Recommendations'>
        <p className='text-xs text-muted-foreground mb-2'>
          After dataset upload, select a task and evaluation procedure.
          Recommendations arrive automatically — do not poll.
        </p>
        <Pre>{`# 2a. Select data science task
WS emit: DataScienceTaskSelected { taskId: "classification" }

# 2b. Select evaluation procedure
WS emit: EvaluationProcedureSelected { id, name }
  -> backend runs recommendation engine
  -> WS receives: state/pipelines/recommendation (ranked list)
  -> wait for this event before selecting a recommendation

# 2c. (Optional) Tune ranking objectives
WS emit: RankingObjectivesChanged { objectives: [...] }
  -> triggers re-ranking
  -> WS receives: state/pipelines/recommendation (updated)`}</Pre>
        <SubSection title='Available catalog (read once at session start)'>
          <Pre>{`GET /catalog              -> full catalog in one request
GET /catalog/tasks       -> data-science tasks
GET /catalog/operators   -> all operators
GET /catalog/evals       -> evaluation procedures
GET /catalog/objectives  -> ranking objectives
GET /catalog/operator-params -> parameter specs + I/O ports`}</Pre>
        </SubSection>
      </Section>

      <Separator />

      {/* ── Flow 3: Build + Execute ──────────────────────── */}
      <Section title='Flow 3: Build, Execute, and Read Results'>
        <p className='text-xs text-muted-foreground mb-2'>
          Pick a recommendation or compose manually. Always save before
          executing. Never fire ExecutePipeline while a run is in progress.
        </p>
        <Pre>{`# 3a. Load a recommendation (preferred)
WS emit: PipelineRecommendationSelected { recommendation }

# 3a-alt. Compose manually (200ms min between events)
WS emit: PipelineNodeAdded { nodeId, nodeType, nodeName, pipelineId }
WS emit: PipelineEdgeAdded { source, target, sourceHandle, targetHandle, pipelineId }
WS emit: PipelineNodeConfigured { nodeId, patchKeys, pipelineId }

# 3b. Save (required before execution)
WS emit: PipelineSaved { pipelineHistory }

# 3c. Execute
WS emit: ExecutePipeline { sessionId, pipelineId }
  -> WS receives: pipeline/run/initialised
  -> WS receives: pipeline/node/started (per node)
  -> WS receives: pipeline/node/completed | pipeline/node/failed (per node)
  -> WS receives: pipeline/run/completed { metrics }
     OR pipeline/run/failed { error }
     OR pipeline/run/error (pre-flight failure)

# 3d. Cancel a running pipeline (if needed)
WS emit: CancelPipeline { sessionId, pipelineId }
  -> WS receives: pipeline/run/cancelled

# 3e. Restore a previous version
WS emit: PipelineVersionRestored { sessionId, pipelineId, versionId }`}</Pre>
        <SubSection title='Execution lifecycle events (listen for these)'>
          <Pre>{`pipeline/run/initialised   run queued
pipeline/run/started      Dask execution began
pipeline/node/started     individual node started
pipeline/node/completed   node succeeded (with duration)
pipeline/node/failed      node failed (with error + trace)
pipeline/node/skipped     node skipped (upstream failure)
pipeline/node/cancelled   node cancelled
pipeline/node/trace-output  model trace (LLM calls)
pipeline/run/completed    run succeeded (with metrics JSON)
pipeline/run/failed       run failed (with error)
pipeline/run/cancelled    run was cancelled
pipeline/run/error        could not start (pre-flight check)
pipeline/rewritten        DAG rewritten by mitigation
queue/status              position + ETA in execution queue
queue/concurrency-limit   concurrency limit reached — back off`}</Pre>
        </SubSection>
      </Section>

      <Separator />

      {/* ── Flow 4: Debugger ─────────────────────────────── */}
      <Section title='Flow 4: Review and Apply Debugger Suggestions'>
        <p className='text-xs text-muted-foreground mb-2'>
          After execution, the debugger emits risk suggestions. Process
          them one at a time — accepting a suggestion rewrites the pipeline.
        </p>
        <Pre>{`# Suggestions arrive automatically after execution:
  -> WS receives: suggestion { id, risk, operator, mitigations }
  -> WS receives: check/started, check/passed | check/failed
  -> WS receives: check/report (consolidated)

# Accept or dismiss a suggestion (one at a time)
WS emit: SuggestionInteraction { suggestionId, action: "accept"|"dismiss" }
  -> if accepted: WS receives pipeline/rewritten { pipeline }
     then WS receives ui/mitigation-applied
  -> if failed: WS receives ui/mitigation-failed

# Data mitigation flow (multi-step)
WS emit: DataMitigationDecision { ... }   -> select mitigation
WS emit: DataMitigationFinish { ... }     -> finalise
WS emit: DataMitigationReset { ... }      -> reset and start over

# Events to listen for:
suggestions/reset       all suggestions cleared
suggestions/revoke      suggestions for a removed operator revoked`}</Pre>
      </Section>

      <Separator />

      {/* ── Flow 5: Knowledge Base ───────────────────────── */}
      <Section title='Flow 5: Query Knowledge Base'>
        <p className='text-xs text-muted-foreground mb-2'>
          KB endpoints are read-only and cached. Safe to call freely, but
          prefer batch reads over per-operator loops.
        </p>
        <Pre>{`# Discover operators for a task
GET /mcp/kb/operators?task=Classification

# Get operator interface + method sequence
GET /mcp/kb/operator/{name}/interface

# Get risks linked to an operator
GET /mcp/kb/operator/{name}/risks

# Get mitigations for a risk
GET /mcp/kb/risk/{name}/mitigations

# Get rewrite annotation for a mitigation
GET /mcp/kb/mitigation/{name}/rewrite

# Full-text search
GET /mcp/kb/search?keyword=fairness&limit=20

# Raw Cypher (read-only, use sparingly)
POST /mcp/kb/query  body: { cypher, parameters }

# KB summary endpoints
GET /mcp/kb/risks        -> all risks
GET /mcp/kb/mitigations  -> all mitigations with risk mappings`}</Pre>
      </Section>

      <Separator />

      {/* ── Flow 6: Rule Authoring ───────────────────────── */}
      <Section title='Flow 6: Author Rewrite Rules'>
        <p className='text-xs text-muted-foreground mb-2'>
          Draft → test → commit cycle. Always test before committing.
          Use the schema endpoint to validate specs client-side first.
        </p>
        <Pre>{`# Get the rule spec schema (do this once)
GET /mcp/schema/rule-spec          -> JSON schema
GET /mcp/schema/rewrite-types      -> available rewrite types

# Create a draft rule
POST /mcp/rule/create  body: { spec }
  -> { draft_id, valid, errors }

# Iterate on the draft
PUT  /mcp/rule/{draft_id}  body: { spec }

# Test against a sample pipeline
POST /mcp/rule/{draft_id}/test  body: { test_dag }
  -> { matched, transformed, diff }

# Commit (only after successful test)
POST /mcp/rule/{draft_id}/commit
  -> rule is now active

# Management
GET    /mcp/rule/drafts        -> list drafts
GET    /mcp/rule/{id}          -> draft detail
DELETE /mcp/rule/{id}          -> delete draft
GET    /mcp/rule/active/list   -> active rules
DELETE /mcp/drafts             -> clear all drafts`}</Pre>
      </Section>

      <Separator />

      {/* ── Flow 7: Mitigation Curation ──────────────────── */}
      <Section title='Flow 7: Propose Mitigations'>
        <p className='text-xs text-muted-foreground mb-2'>
          Similar draft → test → commit cycle. Search existing mitigations
          first to avoid duplicates.
        </p>
        <Pre>{`# Check for existing mitigations
GET /mcp/mitigation/search?text=bias&limit=10

# Propose a new mitigation
POST /mcp/mitigation/propose
  body: { name, description, risks, provenance }

# Annotate with rewrite instructions
PUT /mcp/mitigation/{id}/annotate
  body: { rewrite_type, target, param, value }

# Test against a sample pipeline + operator
POST /mcp/mitigation/{id}/test
  body: { test_dag, operator_fqn }

# Commit to KB
POST /mcp/mitigation/{id}/commit

# Management
GET    /mcp/mitigation/drafts  -> list drafts
GET    /mcp/mitigation/{id}    -> detail
DELETE /mcp/mitigation/{id}    -> delete
GET    /mcp/catalog/mitigation-rewrites -> existing mappings`}</Pre>
      </Section>

      <Separator />

      {/* ── Flow 8: Extraction ───────────────────────────── */}
      <Section title='Flow 8: Extract Pipeline from Code'>
        <p className='text-xs text-muted-foreground mb-2'>
          Convert Python code to a pipeline DAG. The extraction engine
          can be customised with user-defined rules.
        </p>
        <Pre>{`# Extract pipeline from code text
POST /extract  body: { text }
  -> parsed pipeline DAG

# Propose a rule from a correction
POST /extract/propose-rule  body: { correction }

# Run regression tests on rules
POST /extract/regression-test  body: { rules }

# Manage extraction rules
GET  /rules         query: uid       -> user rules
POST /rules         body: { rules, uid }
GET  /extraction/rules               -> versioned rules (paginated)
GET  /extraction/rules/{id}          -> full rule by ID

# WS events for extraction
WS emit: ExtractPipeline { text }
  -> WS receives: extraction/result | extraction/error

WS emit: SaveExtractionRules { rules }
  -> WS receives: extraction/rules-saved

WS emit: SuggestExtractionRules { context }
  -> WS receives: extraction/rules-suggestion | extraction/suggest-error`}</Pre>
      </Section>

      <Separator />

      {/* ── Vault ────────────────────────────────────────── */}
      <Section title='Flow 9: Manage Secrets (Vault)'>
        <p className='text-xs text-muted-foreground mb-2'>
          Environment variables are encrypted client-side (AES-256-GCM).
          The server stores only ciphertext. Check for missing vars before
          executing pipelines that reference them.
        </p>
        <Pre>{`# Check which env vars a pipeline needs
POST /vault/env/check-pipeline  body: { pipeline, uid }
  -> { missing: ["OPENAI_API_KEY", ...] }
  -> also WS receives: vault/check-required

# Store an encrypted env var
POST /vault/env  body: HMAC-sealed envelope
WS emit: VaultEnvVarStored { varName, ciphertext }

# List stored var names (metadata only, never plaintext)
GET /vault/env  query: uid

# Delete a var
DELETE /vault/env/{var_name}  query: uid
WS emit: VaultEnvVarDeleted { varName }`}</Pre>
      </Section>

      <Separator />

      {/* ── Agent Prompts ────────────────────────────────── */}
      <Section title='Agent Workflow Prompts'>
        <p className='text-xs text-muted-foreground mb-2'>
          Pre-built prompts that guide agents through complex multi-step
          workflows. Use these as system prompts for sub-agents.
        </p>
        <Pre>{`GET /mcp/prompts/rule-authoring       query: context?
  -> structured workflow prompt for rule authoring

GET /mcp/prompts/mitigation-curation  query: context?
  -> structured workflow prompt for mitigation curation`}</Pre>
      </Section>

      <Separator />

      {/* ── Observability ────────────────────────────────── */}
      <Section title='Observability (read-only)'>
        <p className='text-xs text-muted-foreground mb-2'>
          Monitor system health. Safe to poll at low frequency (max once
          per 10 seconds). Do not use for real-time state — use WS events.
        </p>
        <Pre>{`GET /observability/system          -> CPU, RSS, disk, queue depth
GET /observability/handlers       -> handler invocation stats
GET /observability/pipelines      -> pipeline run records
GET /observability/throughput     -> event throughput (bucketed)
GET /observability/errors         -> error summary
GET /observability/workers        -> worker host metrics
GET /observability/workers/latest -> most recent worker snapshot
GET /observability/event-map      -> registered event handlers`}</Pre>
      </Section>

      <Separator />

      {/* ── Data Model ───────────────────────────────────── */}
      <Section title='Key Data Structures'>
        <Pre>{`Pipeline DAG (JSON):
  nodes: { [uuid]: Operator | Snippet | Parameter }
  edges: [ { source, destination, position, output } ]

Operator:  { name: "sklearn.X.Y", language: "python", tasks: [...] }
Snippet:   { name, code, language }
Parameter: { name, dtype: "int"|"float"|"string"|"eval"|"env", value }
Edge:      { source: uuid, destination: uuid, position: int|string, output: int }

Session Meta (Redis JSON at session:{id}:meta):
  { uid, dataset: { did, fpath }, pipeline, pipelineHistory,
    selectedTask, selectedEval, rankingObjectives }

Execution (Redis JSON at execution:{run_id}):
  { run_id, session, uid, status, pipeline, metrics, error }`}</Pre>
      </Section>

      <Separator />

      {/* ── Backend → Frontend Events (full list) ────────── */}
      <Section title='All Backend Events (listen only)'>
        <p className='text-xs text-muted-foreground mb-2'>
          Complete list of events the backend may push. Agents should
          handle at minimum: state/*, pipeline/*, error/*, queue/*.
        </p>
        <Pre>{`SESSION SEED (after InitSession)
  state/dataset               dataset metadata
  state/target                target column
  state/selected-task         restore task selection
  state/selected-eval         restore eval selection
  state/lastRun               last execution + metrics
  state/pipeline              pipeline history (all versions)
  state/tasks                 available tasks
  state/operators             available operators
  state/operator-params       parameter specs + I/O ports
  state/evals                 evaluation procedures
  state/objectives            ranking objectives
  state/objectives/selected   selected objectives
  state/objectives/status     objective health
  state/objectives/validation objective compilation errors
  state/adapters              adapter list
  state/queries               pending feedback queries
  state/query                 full query state
  state/quality               dataset quality metrics
  state/quality-checks        quality check results
  state/column-profiles       column statistical profiles
  state/data-mitigation-session  mitigation session state
  state/pipelines/recommendation recommended pipelines
  state/group-created         composite node created
  state/node-created          operator node confirmed
  user/name                   display name
  user/avatar                 avatar URL
  ui/tooltips                 tooltip content
  ui/onboarding               onboarding state

EXECUTION
  pipeline/run/initialised    run queued
  pipeline/run/started        execution began
  pipeline/node/started       node started
  pipeline/node/completed     node done (duration)
  pipeline/node/failed        node error (trace)
  pipeline/node/skipped       upstream failure
  pipeline/node/cancelled     cancelled
  pipeline/node/trace-output  model trace
  pipeline/run/completed      success (metrics)
  pipeline/run/failed         failure (error)
  pipeline/run/cancelled      cancelled
  pipeline/run/error          pre-flight failure
  pipeline/rewritten          DAG rewritten

DEBUGGER
  suggestion                  risk + mitigations
  suggestions/revoke          operator removed
  suggestions/reset           all cleared
  check/started               check started
  check/passed                check passed
  check/failed                check failed
  check/report                consolidated report
  ui/mitigation-applied       mitigation success
  ui/mitigation-failed        mitigation failure

EXTRACTION
  extraction/result           extraction done
  extraction/error            extraction failed
  extraction/rules            rules loaded
  extraction/rules-saved      rules saved
  extraction/rules-suggestion LLM suggestions
  extraction/suggest-error    suggestion failed
  extraction/rule-accepted    rule accepted
  extraction/rule-rejected    rule rejected

PROGRESS
  progress                    metafeature/quality computation

VAULT
  vault/check-required        missing env vars

NOTIFICATIONS
  notification                single notification
  notifications/batch         batch on reconnect

QUEUE
  queue/status                position + ETA
  queue/concurrency-limit     at capacity

ERROR
  error/rate-limited          rate limit hit — back off`}</Pre>
      </Section>
    </div>
  );
}
