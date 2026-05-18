"use client";

// CodeViewer — full-screen read-only Monaco editor overlay.
//
// Rendered via createPortal(document.body) so it escapes any CSS
// transform context (ReactFlow nodes use transforms which break
// `position: fixed` — the modal would be positioned relative to the
// node instead of the viewport).

import React, { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import Editor from "@monaco-editor/react";
import clsx from "clsx";
import { useTheme } from "next-themes";

export default function CodeViewer({
  code = "// Write your code here...",
  language = "javascript",
  show,
  setShow,
}: {
  code?: string;
  language?: string;
  show: boolean;
  setShow: (show: boolean) => void;
}) {
  const { resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  const clickedOutside = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) {
      setShow(false);
    }
  };

  if (!mounted || !show) return null;

  const monacoTheme = resolvedTheme === "dark" ? "vs-dark" : "vs";

  return createPortal(
    <div
      onClick={clickedOutside}
      className='fixed inset-0 z-[100] bg-black/40 flex items-center justify-center'
    >
      <div className='h-3/4 w-3/4 overflow-hidden rounded-xl'>
        <Editor
          key={monacoTheme}
          height='100%'
          language={language}
          value={code}
          theme={monacoTheme}
          options={{
            readOnly: true,
            fontSize: 14,
            minimap: { enabled: false },
            wordWrap: "on",
            automaticLayout: true,
          }}
        />
      </div>
    </div>,
    document.body,
  );
}
