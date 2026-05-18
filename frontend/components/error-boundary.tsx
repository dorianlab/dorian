"use client";

import React from "react";

interface Props {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <div role="alert" aria-live="assertive" className='flex h-full items-center justify-center p-8'>
            <div className='max-w-md space-y-4 text-center'>
              <h2 className='text-lg font-semibold'>Something went wrong</h2>
              <pre className='whitespace-pre-wrap text-sm text-muted-foreground'>
                {this.state.error?.message}
              </pre>
              <button
                className='rounded bg-primary px-4 py-2 text-sm text-primary-foreground'
                onClick={() => this.setState({ hasError: false, error: null })}
              >
                Try again
              </button>
            </div>
          </div>
        )
      );
    }

    return this.props.children;
  }
}
