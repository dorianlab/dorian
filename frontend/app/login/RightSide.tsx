import React from "react";
import { useState } from "react";
import Image from "next/image";
import { signIn } from "next-auth/react";

export default function RightSide() {
  const [isLoading, setIsLoading] = useState(false);
  const [isDemoLoading, setIsDemoLoading] = useState(false);

  const handleGitHubLogin = () => {
    setIsLoading(true);
    signIn("github", { callbackUrl: "/" }).finally(() => {
      setIsLoading(false);
    });
  };

  const handleDemoLogin = () => {
    setIsDemoLoading(true);
    signIn("demo", { username: "demo", callbackUrl: "/" }).finally(() => {
      setIsDemoLoading(false);
    });
  };

  return (
    <div className='flex w-full flex-col items-center justify-center bg-white px-4 lg:w-1/2'>
      <div className='w-full max-w-md'>
        {/* Mobile logo - only visible on small screens */}
        <div className='mb-8 flex items-center justify-center lg:hidden'>
          <div className='relative h-24 w-24'>
            <Image
              src='/dorian-logo.svg'
              alt='Dorian Logo'
              fill
              className='object-contain'
            />
          </div>
          <h1 className='ml-4 text-3xl font-bold text-black'>Dorian</h1>
        </div>

        <div className='rounded-2xl bg-white p-8 shadow-[0_0_60px_-15px_rgba(0,0,0,0.1)]'>
          <div className='relative space-y-6'>
            <div className='space-y-2 text-center'>
              <h2 className='text-2xl font-bold tracking-tight text-black'>
                Welcome to Dorian
              </h2>
              <p className='text-sm text-gray-500'>
                Sign in with GitHub to continue
              </p>
            </div>

            <div className='pt-4 space-y-3'>
              <button
                type='button'
                onClick={handleGitHubLogin}
                disabled={isLoading}
                className='group relative flex w-full items-center justify-center overflow-hidden rounded-lg bg-black px-4 py-3 text-sm font-medium text-white shadow-md transition-all duration-300 animate-fadeIn hover:bg-gray-800 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-70'
              >
                <svg
                  className='mr-2 h-5 w-5 '
                  fill='currentColor'
                  viewBox='0 0 20 20'
                  aria-hidden='true'
                >
                  <path
                    fillRule='evenodd'
                    d='M10 0C4.477 0 0 4.484 0 10.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0110 4.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.203 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.942.359.31.678.921.678 1.856 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0020 10.017C20 4.484 15.522 0 10 0z'
                    clipRule='evenodd'
                  />
                </svg>
                {isLoading ? (
                  <div className='flex items-center'>
                    <svg
                      className='mr-2 h-4 w-4 animate-spin text-white'
                      xmlns='http://www.w3.org/2000/svg'
                      fill='none'
                      viewBox='0 0 24 24'
                    >
                      <circle
                        className='opacity-25'
                        cx='12'
                        cy='12'
                        r='10'
                        stroke='currentColor'
                        strokeWidth='4'
                      ></circle>
                      <path
                        className='opacity-75'
                        fill='currentColor'
                        d='M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z'
                      ></path>
                    </svg>
                    Signing in...
                  </div>
                ) : (
                  "Continue with GitHub"
                )}
              </button>

              {/* Sandbox / preview mock — bypasses GitHub OAuth */}
              <button
                type='button'
                onClick={handleDemoLogin}
                disabled={isDemoLoading}
                className='flex w-full items-center justify-center rounded-lg border border-gray-300 bg-white px-4 py-3 text-sm font-medium text-gray-700 shadow-sm transition-all duration-200 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:opacity-70'
              >
                {isDemoLoading ? "Signing in…" : "Sign in (Demo)"}
              </button>
            </div>
          </div>
        </div>

      </div>
    </div>
  );
}
