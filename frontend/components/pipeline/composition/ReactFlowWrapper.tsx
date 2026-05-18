import { ReactFlowProvider } from "@xyflow/react";
import React from "react";
import "@xyflow/react/dist/style.css";
export default function ReactFlowWrapper({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ReactFlowProvider>{children}</ReactFlowProvider>;
}
