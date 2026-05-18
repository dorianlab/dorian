## Where to Edit What (Task-Based Guide)

### 1 Pipeline UI (Canvas, Nodes, Sidebar, Drag & Drop)

If you want to change how the pipeline looks or behaves in the editor:

- **Canvas & layout**
  - `components/pipeline/composition/canvas/`
  - Look here for: pipelineCanvas, pipelineLayout, pipelineRecommendation, etc.
  - Helpers in: `/helpers/pipeline.ts`
  - Hooks in: `/hooks/usePipelineComposition.ts`

- **Nodes (operator / parameter / snippet / visualizer)**
  - `components/pipeline/composition/Nodes/`
  - Edit node UI, node behavior, and node-specific controls here.

- **Edges**
  - `components/pipeline/composition/Edges/`
  - Change labeled edges, edge styling, edge UI logic here.

- **Sidebar (operators list / drag sources / panels)**
  - `components/pipeline/composition/sidebar/`
  - `components/pipeline/composition/DroppableSidebar.tsx`

- **DnD + drag context**
  - `components/pipeline/composition/DndContext.tsx`

- **Pipeline forms (create/edit operator, parameter, snippet, etc.)**
  - `components/pipeline/composition/Forms/`

---

### 2 Socket Events (Inbound + Outbound)

If you want to change what happens when the frontend receives websocket events:

- **Socket hook (main inbound handler / wiring)**
  - `hooks/usePipelineSocket.ts`

- **Event names/contracts + helpers**
  - `helpers/ws-events.ts`

- **Websocket store (state updates based on events)**
  - `store/web-socket.ts`
- **Websocket types **
  - `/helpers/types.ts`

> Typical workflow: for sending events, add/adjust them in `helpers/ws-events.ts` → handle type in `/helpers/types.ts`, for handling events update `hooks/usePipelineSocket.ts`

---

### 3 Session History (Chat sessions / pipeline history / previous runs)

If you want to change history tracking, session lists, or session switching:

- **Session list + selection logic**
  - `hooks/useChatSessions.ts`

- **Sidebar session UI**
  - `components/layout/sidebar/`
  - (The chat/session list UI usually lives here.)

- **History-related backend calls / BFF endpoints**
  - `app/api/sessions.ts`

- **Any “pipeline history” structure passed around**
  - Check:
    - `app/state.ts`
    - `app/interfaces.ts`
    - `helpers/types.ts`

> Typical workflow: UI session selection (sidebar) → session hook (`useChatSessions.ts`) → fetch from `app/api/sessions.ts` → hydrate state used by the pipeline.

---

---

### 4 Common UI Building Blocks

If you want to adjust generic UI components used everywhere:

- **shadcn/ui components**
  - `components/ui/`

---

### 5 Frontend API Routes (Next.js route handlers / BFF layer)

If you want to change how the frontend talks to backend HTTP endpoints:

- `app/api/pipeline.ts`
- `app/api/dataset.ts`
- `app/api/sessions.ts`
- `app/api/suggestions.ts`
- `app/api/feedback.ts`
- Auth route:
  - `app/api/auth/[...nextauth]/route.ts`
