import React from "react";
import Image from "next/image";
import logo from "@/app/logo.svg";
export default function LeftSide() {
  return (
    <div className='hidden w-1/2 bg-gradient-to-br from-[#2c4f8e] to-[#7794c9] lg:block'>
      <div className='flex h-full flex-col items-center justify-center px-12'>
        <div className='relative h-40 w-40'>
          <Image src={logo} alt='Dorian Logo' fill className='object-contain' />
        </div>
        <h1 className='mt-8 text-4xl font-bold text-white'>Dorian</h1>
        <p className='mt-4 text-center text-lg w-2/3 text-[#cedaf0]'>
          A powerful platform for developers to collaborate, create, and
          innovate.
        </p>

        {/* Decorative elements */}
        <div className='mt-12 grid grid-cols-2 gap-4'>
          {[1, 2, 3, 4].map((i) => (
            <div
              key={i}
              className='h-24 w-24 rounded-lg bg-[#cedaf0]/20 backdrop-blur-sm'
              aria-hidden='true'
            />
          ))}
        </div>
      </div>
    </div>
  );
}
