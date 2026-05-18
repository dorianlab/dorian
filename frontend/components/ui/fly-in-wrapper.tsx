// FlyInWrapper.tsx
"use client";

import { PropsWithChildren } from "react";
import { MotionConfig, AnimatePresence, motion } from "framer-motion";

type Props = PropsWithChildren<{
  /** control visibility for enter/exit */
  isVisible: boolean;
  /** Use translate only on the overlay (not on RF container) */
  x?: string | number;
  className?: string;
}>;

export default function FlyInWrapper({
  children,
  isVisible,
  x = "-100vw",
  className,
}: Props) {
  return (
    <MotionConfig reducedMotion='never'>
      <div className={(className ?? "") + " relative min-h-0 h-full w-full"}>
        <AnimatePresence mode='wait'>
          {isVisible && (
            <motion.div
              key='fly-in-wrapper'
              initial={{ opacity: 0, x }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x }}
              transition={{ duration: 1.35, ease: [0.22, 1, 0.36, 1] }}
              className='absolute inset-0'
              style={{
                willChange: "transform, opacity",
                pointerEvents: "auto",
              }}
            >
              {children}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </MotionConfig>
  );
}
