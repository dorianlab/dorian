import { Button } from "@/components/ui/button";
import { Search } from "lucide-react";
import React from "react";

function SearchBar({ onActivate }: { onActivate: () => void }) {
  return (
    <Button
      variant='outline'
      className='w-full h-10 my-2 font-normal text-sm justify-start text-muted-foreground'
      onClick={onActivate}
    >
      <Search className=' h-4 w-4' /> Search or type a command...
    </Button>
  );
}

export default SearchBar;
