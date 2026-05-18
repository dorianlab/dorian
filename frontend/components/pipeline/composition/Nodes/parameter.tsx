import React, { useCallback, useEffect, useState, useMemo } from "react";
import { useReactFlow, Position } from "@xyflow/react";
import HandleRenderer from "./HandleRenderer";
import { ParameterProps } from "@/types/ui";
import { useUIStore } from "@/store/ui";
import NodeWrapper, { inferStatus } from "./wrapper";
import { useNodeHandles } from "@/hooks/useNodeHandles";
import { useVaultStore } from "@/store/vault";
import { useSessionStore } from "@/store/session";

/** Minimum width so short parameter names (e.g. "top_p") still show readable input. */
const MIN_PARAM_W = 170;
/** Maximum width — prevents a single long value from blowing out the layout. */
const MAX_PARAM_W = 380;
/** Approximate px per character for the 14px medium-weight title font. */
const PX_PER_CHAR = 9;
/** Extra padding for node chrome (border, inner padding, delete button zone). */
const NODE_CHROME_PX = 56;
/** Names beyond this length get truncated with a tooltip. */
const TRUNCATE_CHARS = 36;

/**
 * Width driven by the longer of name / value so the input field is readable.
 * Clamped to [MIN_PARAM_W, MAX_PARAM_W].
 */
function computeParamWidth(name: string, value: string): number {
  const displayName = name.length > TRUNCATE_CHARS ? name.slice(0, TRUNCATE_CHARS) : name;
  const nameW = displayName.length * PX_PER_CHAR + NODE_CHROME_PX;
  // Value is shown in an <input> with its own padding (~24px) + ~8px/char.
  const valueW = value.length * 8 + NODE_CHROME_PX + 24;
  return Math.min(MAX_PARAM_W, Math.max(MIN_PARAM_W, nameW, valueW));
}

function ParameterNode({ data }: ParameterProps) {
  const { direction } = useUIStore();
  const { deleteElements } = useReactFlow();
  const isTB = direction === "TB";
  const isEnvVar = data.type === "env";
  // Env-var nodes need room for the "Select env var..." button + name header.
  const ENV_MIN_W = 190;
  const paramWidth = isEnvVar
    ? Math.max(ENV_MIN_W, (data.name ?? "").length * PX_PER_CHAR + 60)
    : computeParamWidth(data.name ?? "", data.value ?? "");

  const { sources } = useNodeHandles({
    nodeId: data.uuid,
    outputs: data.outputs,
    isNewNode: data.isNewNode,
  });

  const onChange = useCallback(
    (evt: React.ChangeEvent<HTMLInputElement>) => {
      data.updateNodeData?.(data.uuid, { value: evt.target.value });
    },
    [data.updateNodeData, data.uuid],
  );

  return (
    <NodeWrapper
      title={isEnvVar ? `\u{1F512} ${data.name}` : data.name}
      status={inferStatus(data)}
      errorMessage={data.execError}
      errorTrace={data.execTrace}
      startTime={data.execStartTime}
      duration={data.execDuration}
      onDelete={() => deleteElements({ nodes: [{ id: data.uuid }] })}
      style={{ width: paramWidth, minWidth: MIN_PARAM_W }}
    >
      {isEnvVar ? (
        <EnvVarInput data={data} />
      ) : (
        <input
          id={`param-${data.uuid}`}
          aria-label={`Value for ${data.name}`}
          className='nodrag w-full mb-1 mt-2 p-2 px-3 border border-input rounded-md bg-background text-foreground'
          value={data.value ?? ""}
          onChange={onChange}
        />
      )}

      {/* Parameters are value sources — output only, no input handles. */}
      <HandleRenderer
        type='source'
        items={sources}
        nodeType='parameter'
        position={isTB ? Position.Bottom : Position.Right}
      />
    </NodeWrapper>
  );
}

/**
 * Env var parameter input — shows a dropdown of vault variables instead of
 * a freeform text field.  The stored value is ``${VAR_NAME}`` — never the
 * actual secret.
 */
function EnvVarInput({ data }: { data: ParameterProps["data"] }) {
  const { envVars, fetchEnvVars, loading } = useVaultStore();
  const { userId } = useSessionStore();
  const [isEditing, setIsEditing] = useState(false);

  // Fetch vault vars when the dropdown opens (if not already loaded).
  useEffect(() => {
    if (isEditing && userId && envVars.length === 0 && !loading) {
      fetchEnvVars(userId);
    }
  }, [isEditing, userId, envVars.length, loading, fetchEnvVars]);

  // Extract current var name from ${VAR_NAME} format
  const currentVar = useMemo(() => {
    const v = data.value ?? "";
    if (v.startsWith("${") && v.endsWith("}")) return v.slice(2, -1);
    return v;
  }, [data.value]);

  const handleSelect = useCallback(
    (varName: string) => {
      data.updateNodeData?.(data.uuid, { value: `\${${varName}}` });
      setIsEditing(false);
    },
    [data.updateNodeData, data.uuid],
  );

  const handleManualEntry = useCallback(
    (evt: React.ChangeEvent<HTMLInputElement>) => {
      const raw = evt.target.value.replace(/[^A-Za-z0-9_\-\.]/g, "");
      data.updateNodeData?.(data.uuid, { value: raw ? `\${${raw}}` : "" });
    },
    [data.updateNodeData, data.uuid],
  );

  // Close the editing dropdown — but only if focus leaves the entire container
  const containerRef = React.useRef<HTMLDivElement>(null);
  const handleBlur = useCallback(
    (e: React.FocusEvent) => {
      // If focus moved to another element inside the container, keep editing
      setTimeout(() => {
        if (
          containerRef.current &&
          !containerRef.current.contains(document.activeElement)
        ) {
          setIsEditing(false);
        }
      }, 150);
    },
    [],
  );

  return (
    <div className="nodrag w-full mb-1 mt-2">
      {/* Current value display / click to edit */}
      {!isEditing ? (
        <button
          onClick={() => setIsEditing(true)}
          className="w-full p-2 px-3 border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 rounded-md text-left text-xs font-mono hover:border-amber-400 dark:hover:border-amber-600 transition-colors"
          title="Click to select an environment variable from your vault"
        >
          {currentVar ? (
            <span className="flex items-center gap-1.5 overflow-hidden">
              <span className="text-amber-600 dark:text-amber-400 shrink-0">{"${"}</span>
              <span className="text-amber-800 dark:text-amber-300 truncate">{currentVar}</span>
              <span className="text-amber-600 dark:text-amber-400 shrink-0">{"}"}</span>
            </span>
          ) : (
            <span className="text-amber-400 italic">Select env var...</span>
          )}
        </button>
      ) : (
        <div ref={containerRef} className="space-y-1" onBlur={handleBlur}>
          {/* Vault variables dropdown */}
          {loading ? (
            <div className="px-3 py-2 text-xs text-amber-500 italic">Loading vault…</div>
          ) : envVars.length > 0 ? (
            <div className="max-h-24 overflow-y-auto border border-amber-200 dark:border-amber-800 rounded-md bg-card">
              {envVars.map((v) => (
                <button
                  key={v.name}
                  onClick={() => handleSelect(v.name)}
                  className={`w-full text-left px-3 py-1.5 text-xs font-mono hover:bg-amber-50 dark:hover:bg-amber-950/40 transition-colors ${
                    v.name === currentVar
                      ? "bg-amber-100 dark:bg-amber-900/50 text-amber-800 dark:text-amber-300"
                      : "text-gray-700 dark:text-gray-300"
                  }`}
                >
                  {v.name}
                </button>
              ))}
            </div>
          ) : (
            <div className="px-3 py-2 text-xs text-muted-foreground italic">
              No vault variables yet — add via User Menu → Environment Variables
            </div>
          )}
          {/* Manual entry fallback */}
          <input
            type="text"
            placeholder="Or type a variable name..."
            className="w-full p-2 px-3 border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 rounded-md text-xs font-mono text-foreground"
            value={currentVar}
            onChange={handleManualEntry}
            autoFocus
          />
        </div>
      )}
    </div>
  );
}

export default React.memo(ParameterNode);
