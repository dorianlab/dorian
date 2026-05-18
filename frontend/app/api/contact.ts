import { createApiClient } from "@/lib/api-client";
import env from "@/env.config";

const contactApi = createApiClient({
  baseURL: `${env.backend}/contact`,
});

export async function submitBugReport(
  uid: string,
  data: {
    name: string;
    title: string;
    description: string;
    steps: string;
    expected: string;
    device: string;
    severity: string;
  },
  files: File[],
) {
  const form = new FormData();
  form.append("uid", uid);
  if (data.name) form.append("name", data.name);
  form.append("title", data.title);
  form.append("description", data.description);
  form.append("steps", data.steps);
  form.append("expected", data.expected);
  form.append("device", data.device);
  form.append("severity", data.severity);
  for (const file of files) {
    form.append("files", file);
  }
  const res = await contactApi.post("/bug", form);
  return res.data as { status: string; submission_id: string };
}

export async function submitFeedback(
  uid: string,
  data: {
    name: string;
    type: string;
    subject: string;
    details: string;
    rating: string;
  },
) {
  const form = new FormData();
  form.append("uid", uid);
  if (data.name) form.append("name", data.name);
  form.append("feedback_type", data.type);
  form.append("subject", data.subject);
  form.append("details", data.details);
  form.append("rating", data.rating);
  const res = await contactApi.post("/feedback", form);
  return res.data as { status: string; submission_id: string };
}

export async function submitContactUs(
  uid: string,
  data: {
    firstName: string;
    lastName: string;
    email: string;
    subject: string;
    message: string;
  },
) {
  const form = new FormData();
  form.append("uid", uid);
  form.append("first_name", data.firstName);
  form.append("last_name", data.lastName);
  form.append("email", data.email);
  form.append("subject", data.subject);
  form.append("message", data.message);
  const res = await contactApi.post("/us", form);
  return res.data as { status: string; submission_id: string };
}
