"use client";

import { useEffect, useState } from "react";
import Editor from "@monaco-editor/react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const languageOptions = [
  { label: "JavaScript", value: "javascript" },
  { label: "TypeScript", value: "typescript" },
  { label: "Python", value: "python" },
  { label: "Java", value: "java" },
  { label: "C++", value: "cpp" },
  { label: "C#", value: "csharp" },
  { label: "Go", value: "go" },
  { label: "Ruby", value: "ruby" },
  { label: "PHP", value: "php" },
] as const;

type Lang = (typeof languageOptions)[number]["value"];

export default function MonacoEditor() {
  const [language, setLanguage] = useState<Lang>("javascript");
  const [code, setCode] = useState("// Write your code here...");
  const [permission, setPermission] = useState(false);

  const handleSave = () => {
    // TODO: implement save logic
  };

  const handleCancel = () => {
    setPermission(false);
    setLanguage("javascript");
    setCode("// JavaScript code here...");
  };

  useEffect(() => {
    // Set a default snippet depending on language
    switch (language) {
      case "javascript":
        setCode("// JavaScript code here...");
        break;
      case "typescript":
        setCode("// TypeScript code here...");
        break;
      case "python":
        setCode("# Python code here...");
        break;
      case "java":
        setCode("// Java code here...");
        break;
      case "cpp":
        setCode("// C++ code here...");
        break;
      case "csharp":
        setCode("// C# code here...");
        break;
      case "go":
        setCode("// Go code here...");
        break;
      case "ruby":
        setCode("# Ruby code here...");
        break;
      case "php":
        setCode("// PHP code here...");
        break;
      default:
        setCode("// Write your code here...");
        break;
    }
  }, [language]);

  return (
    <div className='p-4 space-y-4'>
      {/* Language selector */}
      <div className='space-y-2'>
        <Label className='font-semibold'>Language</Label>
        <Select value={language} onValueChange={(v) => setLanguage(v as Lang)}>
          <SelectTrigger>
            <SelectValue placeholder='Select language' />
          </SelectTrigger>
          <SelectContent>
            {languageOptions.map((lang) => (
              <SelectItem key={lang.value} value={lang.value}>
                {lang.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Monaco Editor */}
      <div className='h-[400px] border rounded-md overflow-hidden'>
        <Editor
          height='100%'
          language={language}
          value={code}
          onChange={(val) => setCode(val || "")}
          theme='light'
          options={{
            fontSize: 14,
            minimap: { enabled: false },
            wordWrap: "on",
            automaticLayout: true,
          }}
        />
      </div>

      {/* Permission checkbox */}
      <div className='flex items-center gap-2'>
        <Checkbox
          id='permission'
          checked={permission}
          onCheckedChange={(checked) => setPermission(checked === true)}
        />
        <Label htmlFor='permission' className='text-sm font-normal'>
          I allow use of my code/data for improvements.
        </Label>
      </div>

      {/* Action buttons */}
      <div className='flex gap-2'>
        <Button onClick={handleSave}>Save</Button>
        <Button variant='outline' onClick={handleCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
