import React from "react";
import { Handle, Position } from "@xyflow/react";

type HandleItem = {
  position?: string | number; // <-- IMPORTANT: can be "bootstrap" etc
  name?: string;
  label?: string;
  type?: string; // data type (e.g. "int", "float", "array") — fallback label
};

/** True when `s` is a bare numeric index like "0", "1", "2". */
function isBareNumeric(s: string): boolean {
  return /^\d+$/.test(s);
}

function HandleRenderer({
  items,
  type,
  position,
  nodeType,
}: {
  items: HandleItem[];
  type: "source" | "target";
  position: Position;
  nodeType: string;
}) {
  const total = items.length;
  // Rotate labels vertically when ≥3 handles to prevent overlap
  const useVertical = total >= 3;
  const isHorizontalAxis =
    position === Position.Top || position === Position.Bottom;

  return items.map((item, index) => {
    const offset = ((index + 1) / (total + 1)) * 100;

    const style = isHorizontalAxis
      ? { left: `${offset}%` }
      : { top: `${offset}%` };

    // Port identifier used as the ReactFlow handle id (connectivity key).
    // Must remain stable — edge wiring depends on this.
    const handleId = String(
      item?.name ?? item?.label ?? item?.position ?? index,
    );

    // Display label resolution:
    //   1. Explicit ``label`` (KB-supplied semantic label).
    //   2. ``name`` if non-numeric (already-semantic handles from KB).
    //   3. ``type`` if non-generic (fallback for untyped handles).
    //   4. Bare numeric with arrow prefix ("→0") -- last-resort, so
    //      users see SOMETHING. Numeric names indicate a KB gap that
    //      the backend now surfaces as a KBPortNameGap event.
    const rawName = item?.name ?? "";
    const nameLabel =
      rawName && !isBareNumeric(rawName) ? rawName : null;
    const typeLabel =
      item?.type &&
      !["any", "Array", "DataFrame", "Model", "string"].includes(item.type)
        ? item.type
        : null;
    const displayLabel =
      item?.label ||
      nameLabel ||
      typeLabel ||
      (rawName ? `→${rawName}` : `→${index}`);

    const spanStyle: React.CSSProperties = {
      position: "absolute",
      fontSize: "10px",
      padding: "2px 4px",
      whiteSpace: "nowrap",
      pointerEvents: "none",
      // Vertical text when many handles, otherwise centered horizontally
      ...(useVertical
        ? { writingMode: "vertical-rl", textOrientation: "mixed" }
        : {}),
      // Offset transform: centre the label relative to the handle dot
      transform: useVertical ? "translateX(-50%)" : "translate(-50%, -50%)",
      ...(position === Position.Top && {
        bottom: "100%",
        marginBottom: useVertical ? "2px" : "0px",
      }),
      ...(position === Position.Bottom && {
        top: "100%",
        marginTop: useVertical ? "2px" : "6px",
      }),
      ...(position === Position.Left && {
        right: "100%",
        marginRight: "2px",
      }),
      ...(position === Position.Right && {
        left: "100%",
        marginLeft: "2px",
      }),
    };

    return (
      <Handle
        key={`${type}-${position}-${handleId}`}
        id={handleId}
        type={type}
        position={position}
        style={style}
      >
        {nodeType !== "parameter" && (
          <span style={spanStyle} className='text-muted-foreground'>
            {displayLabel}
          </span>
        )}
      </Handle>
    );
  });
}

export default React.memo(HandleRenderer);
