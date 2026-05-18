"use client";

import { useState } from "react";
import { Info, X, Bug, MessageSquarePlus, Lightbulb, Wrench } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";

export function AboutDorianDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className='w-[95vw] max-w-7xl max-h-[85vh] overflow-y-auto'
        aria-describedby={undefined}
      >
        <DialogHeader>
          <DialogTitle className='text-xl font-serif'>
            Welcome to the Dorian Alpha
          </DialogTitle>
        </DialogHeader>

        <div className='space-y-5 text-sm leading-relaxed text-foreground/90'>
          {/* ── What is Dorian ───────────────────────────────────── */}
          <section>
            <h3 className='font-semibold text-base mb-1.5'>What is Dorian?</h3>
            <p>
              Dorian is an interactive system for building trustworthy data
              science pipelines. Instead of writing code from scratch, you
              compose pipelines visually by connecting operators on a canvas
              &mdash; from data loading and preprocessing to model training and
              evaluation.
            </p>
            <p className='mt-2'>
              A built-in debugger continuously analyses your pipeline for
              risks (bias, data quality, overfitting) using deterministic,
              rule-based checks and suggests mitigations that are applied
              directly to the pipeline graph &mdash; keeping you in control
              while automating the tedious parts.
            </p>
          </section>

          {/* ── Key features ────────────────────────────────────── */}
          <section>
            <h3 className='font-semibold text-base mb-1.5'>Key features</h3>
            <ul className='list-disc ml-5 space-y-1'>
              <li>
                <strong>Visual pipeline builder</strong> &mdash; drag operators
                (sklearn, pandas, LLMs, guardrails) onto the canvas and connect
                them
              </li>
              <li>
                <strong>Intelligent recommendations</strong> &mdash; get ranked
                candidate pipelines tailored to your data and task
              </li>
              <li>
                <strong>Risk debugger</strong> &mdash; deterministic,
                rule-based identification of fairness, data quality, and
                robustness issues with one-click mitigations
              </li>
              <li>
                <strong>Flexible evaluation</strong> &mdash; define success
                criteria your way, including custom metrics and domain-specific
                procedures
              </li>
              <li>
                <strong>Interactive refinement</strong> &mdash; edit, test, and
                iterate on pipelines in real time
              </li>
            </ul>
          </section>

          {/* ── Alpha testing CTA ───────────────────────────────── */}
          <section className='rounded-lg border border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/30 p-4'>
            <h3 className='font-semibold text-base mb-1.5 flex items-center gap-2'>
              <Wrench className='h-4 w-4 text-amber-600 dark:text-amber-400' />
              Your input shapes what comes next
            </h3>
            <p>
              Dorian is still early &mdash; this alpha is all about learning
              from real usage. We&apos;d love for you to explore freely, try
              things out, and share what you find. Your feedback at this stage
              has an outsized impact on the direction of the product.
            </p>
            <p className='mt-2'>
              We especially welcome:
            </p>
            <ul className='list-none mt-2 space-y-2'>
              <li className='flex items-start gap-2'>
                <Bug className='h-4 w-4 mt-0.5 text-red-500 shrink-0' />
                <span>
                  <strong>Bugs &amp; broken things</strong> &mdash; anything that
                  doesn&apos;t work as expected, crashes, or looks wrong
                </span>
              </li>
              <li className='flex items-start gap-2'>
                <MessageSquarePlus className='h-4 w-4 mt-0.5 text-blue-500 shrink-0' />
                <span>
                  <strong>Feature requests</strong> &mdash; capabilities you wish
                  existed, workflows you find clunky
                </span>
              </li>
              <li className='flex items-start gap-2'>
                <Lightbulb className='h-4 w-4 mt-0.5 text-yellow-500 shrink-0' />
                <span>
                  <strong>Day-to-day usefulness</strong> &mdash; what would make
                  Dorian a tool you actually reach for in your daily work?
                </span>
              </li>
            </ul>
          </section>

          {/* ── How to give feedback ────────────────────────────── */}
          <section className='rounded-lg border border-primary/20 bg-primary/5 p-4'>
            <h3 className='font-semibold text-base mb-1.5 flex items-center gap-2'>
              <Bug className='h-4 w-4 text-primary' />
              How to give feedback
            </h3>
            <p>
              Use the <strong>bug button</strong>{" "}
              <span className='inline-flex items-center justify-center rounded-full bg-primary/10 border border-primary/20 px-1.5 py-0.5 text-xs font-medium'>
                <Bug className='h-3 w-3' />
              </span>{" "}
              on the right side of the screen. It&apos;s always available, no
              matter where you are in the app. Click it to report bugs, suggest
              features, or share any observation.
            </p>
            <p className='mt-2'>
              Every piece of feedback is read by the development team. There is
              no feedback too small or too obvious &mdash; if something feels
              off, we want to know.
            </p>
          </section>

          {/* ── Getting started ─────────────────────────────────── */}
          <section>
            <h3 className='font-semibold text-base mb-1.5'>Getting started</h3>
            <ol className='list-decimal ml-5 space-y-1'>
              <li>Create a session from the sidebar</li>
              <li>Upload a CSV dataset</li>
              <li>Select a data science task (classification, regression, ...)</li>
              <li>Pick a recommended pipeline or compose one from scratch</li>
              <li>
                Run it &mdash; the debugger will flag risks and suggest
                improvements
              </li>
              <li>Iterate, compare, and refine</li>
            </ol>
          </section>
        </div>

        <div className='flex justify-end pt-2'>
          <Button onClick={() => onOpenChange(false)}>Got it</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function AboutDorianButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              onClick={() => setOpen(true)}
              className='inline-flex items-center justify-center rounded-full hover:bg-white/20 p-0.5 transition-colors'
            >
              <Info className='h-3.5 w-3.5' />
            </button>
          </TooltipTrigger>
          <TooltipContent>About Dorian</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <AboutDorianDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
