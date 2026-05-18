import { apiClient } from "@/lib/api-client";

export interface InteractionPayload {
  suggestion_id: string;
  uid: string;
  session: string;
  type: "accept" | "reject" | "upvote" | "downvote";
  suggestion: Record<string, any>;
}

export async function recordSuggestionInteraction(
  interaction: InteractionPayload
): Promise<void> {
  try {
    await apiClient.post("/suggestion/interaction", interaction, {
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    console.error("Error sending interaction:", err);
    throw err;
  }
}
