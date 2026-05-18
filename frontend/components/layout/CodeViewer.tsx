"use client";

import React from "react";
import Editor from "@monaco-editor/react";
import { useTheme } from "next-themes";
import clsx from "clsx";

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

  const clickedOutside = (e: React.MouseEvent) => {
    if (
      (e.target as HTMLElement).className ===
      "bg-black/40 flex items-center justify-center fixed top-0 left-0  !h-screen w-screen !z-[100]"
    ) {
      setShow(false);
    }
  };

  return (
    <div
      onClick={clickedOutside}
      className={clsx(
        "bg-black/40 flex items-center justify-center fixed top-0 left-0  !h-screen w-screen !z-[100]",
        !show && "hidden",
      )}
      style={{ height: "100%" }}
    >
      <div className='h-3/4 w-3/4'>
        <Editor
          height='100%'
          language={language}
          value={code}
          onChange={(val) => {}}
          theme={resolvedTheme === "dark" ? "vs-dark" : "light"}
          options={{
            fontSize: 14,
            minimap: { enabled: false },
            wordWrap: "on",
            automaticLayout: true,
          }}
        />
      </div>
    </div>
  );
}
