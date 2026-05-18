// 'use client'

// import { signIn } from "next-auth/react";

// export default function LoginPage() {
//   return (
//     <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100vh" }}>
//       <h1>Login</h1>
//       <button
//         onClick={() => signIn("github", { callbackUrl: "/" })}
//         style={{
//           padding: "10px 20px",
//           fontSize: "16px",
//           cursor: "pointer",
//           borderRadius: "5px",
//           border: "1px solid #000",
//           backgroundColor: "#333",
//           color: "#fff",
//           marginTop: "20px",
//         }}
//       >
//         Sign in with GitHub
//       </button>
//     </div>
//   );
// }

"use client";

import { useState } from "react";
import Image from "next/image";
import Link from "next/link";
import logo from "@/app/logo.svg";
import { signIn } from "next-auth/react";
import LeftSide from "./LeftSide";
import RightSide from "./RightSide";

export default function LoginPage() {
  return (
    <div className='flex min-h-screen w-full bg-gray-50'>
      <LeftSide />

      <RightSide />
    </div>
  );
}
