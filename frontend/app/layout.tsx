import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'

const inter = Inter({ subsets: ['latin'] })

export const metadata: Metadata = {
  title: 'Dorian Studio',
  description: 'TBD',
  icons: {
    icon: 'http://127.0.0.1:8000/favicon.ico',
  },
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html className="h-full bg-white" lang="en">
      <body className={inter.className.concat(" h-full")}>{children}</body>
    </html>
  )
}
