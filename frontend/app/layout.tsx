import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import AuthProvider from "@/context/AuthProvider";
import { ThemeProvider } from "@/context/ThemeProvider";
import { Toaster } from "@/components/ui/sonner";
import AlphaBanner from "@/components/AlphaBanner";
import ConnectivityBanner from "@/components/ConnectivityBanner";
import UserSync from "@/components/UserSync";
const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Dorian Studio",
  description: "TBD",
  // icons: {
  //   icon: '',
  // },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <html className='h-full' lang='en' suppressHydrationWarning>
        <body className={inter.className.concat(" h-full")}>
          <ThemeProvider attribute='class' defaultTheme='system' enableSystem>
            <div className='flex flex-col h-full'>
              <AlphaBanner />
              <AuthProvider>
                <UserSync />
                <ConnectivityBanner />
                <div className='flex-1 min-h-0'>{children}</div>
              </AuthProvider>
            </div>
            <Toaster />
          </ThemeProvider>
        </body>
      </html>
    </>
  );
}
