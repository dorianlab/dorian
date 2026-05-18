// app/home/Home.tsx
"use client";
import { useEffect } from "react";
import { useSession } from "next-auth/react";
import { useRouter } from "next/navigation";
import { useUIStore } from "@/store/ui";
import { useSessionStore } from "@/store/session";
import { usePipelineStore } from "@/store/pipeline";
import { usePipelineSocket } from "@/hooks/usePipelineSocket";
import PipelineComposition from "@/components/pipeline/composition/canvas/index";
import ContactForm from "@/components/ContactForm";
import CodeViewer from "@/components/ui/code-viewer";

import { Toaster } from "react-hot-toast";
import { useRecommendationEngineStore } from "@/store/recommendation-engine";
import FeedbackModal from "@/components/FeedbackLoop/feedback-modal";
import AgentPanel from "@/components/agent-panel";
import { MissingEnvVarsDialog } from "@/components/vault";
import { ObjectivesConflictDialog } from "@/components/objectives/conflict-dialog";
import ErrorBoundary from "@/components/error-boundary";

export default function Home() {
  const router = useRouter();
  const { status } = useSession();

  const { activeSessionId } = useSessionStore();
  const { setPipelineHistory } = usePipelineStore();

  const {
    code,
    language,
    showCodeViewer,
    setShowCodeViewer,
    queries,
    feedbackModalOpen,
    setFeedbackModalOpen,
  } = useUIStore();

  // Redirect if unauthenticated
  useEffect(() => {
    if (status === "unauthenticated") router.push("/login");
  }, [status, router]);

  // Clear pipeline when there is no active session
  useEffect(() => {
    if (!activeSessionId) setPipelineHistory(null);
  }, [activeSessionId, setPipelineHistory]);

  usePipelineSocket({
    onQueries: (nextQueries) => {
      setFeedbackModalOpen(nextQueries.length > 0 && nextQueries[0]?.question !== "");
    },
  });

  useEffect(() => {
    if (queries.length > 0) {
      setFeedbackModalOpen(true);
    }
  }, [queries, setFeedbackModalOpen]);

  if (status === "loading" || status === "unauthenticated") return null;

  return (
    <ErrorBoundary>
      <div className='flex relative h-full flex-row'>
        {/* Overlays — positioned outside the flex layout flow so they
            never steal width from <main>. ContactForm's GuidedTooltip
            wrapper would otherwise become a flex sibling of <main>. */}
        <div className='absolute inset-0 pointer-events-none z-20'>
          <div className='pointer-events-auto'>
            <FeedbackModal
              isOpen={feedbackModalOpen}
              onClose={() => setFeedbackModalOpen(false)}
            />
          </div>
          <div className='pointer-events-auto'>
            <ContactForm />
          </div>
        </div>

        <AgentPanel />
        <MissingEnvVarsDialog />
        <ObjectivesConflictDialog />

        <CodeViewer
          language={language}
          code={code}
          show={showCodeViewer}
          setShow={setShowCodeViewer}
        />

        <main className='w-full flex flex-col h-full relative z-10'>
          <PipelineComposition />
        </main>

        <Toaster />
      </div>
    </ErrorBoundary>
  );
}
