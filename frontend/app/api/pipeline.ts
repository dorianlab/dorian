import { apiClient } from "@/lib/api-client";

export async function importPipeline(
  file: File,
  setProgress: (value: number) => void,
  sessionId: string,
  userId: string
) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("session_id", sessionId);
  formData.append("user_id", userId);
  return apiClient
    .post("/import", formData, {
      headers: {
        "content-type": "multipart/form-data",
        "Access-Control-Allow-Origin": "*",
      },
      onUploadProgress: (p) => {
        setProgress(Math.round((100 * p.loaded) / p.total!));
      },
    })
    .then(() => {
      setProgress(100);
    });
}
