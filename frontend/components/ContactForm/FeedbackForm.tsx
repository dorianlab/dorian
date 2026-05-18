"use client";

import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import clsx from "clsx";
import { FrownIcon, MehIcon, SmileIcon, Laugh, Angry } from "lucide-react";
import { submitFeedback } from "@/app/api/contact";
import { useSessionStore } from "@/store/session";
import { toast } from "sonner";
import { isRateLimitError } from "@/lib/api-client";

export default function FeedbackForm() {
  const userId = useSessionStore((s) => s.userId);
  const [feedbackFormData, setFeedbackFormData] = useState({
    name: "",
    type: "suggestion",
    subject: "",
    details: "",
    rating: "5",
  });

  const [loading, setLoading] = useState(false);

  const handleFeedbackSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await submitFeedback(userId, feedbackFormData);
      toast.success("Feedback submitted", {
        description: "Thanks for your feedback! We appreciate it.",
      });
      setFeedbackFormData({
        name: "",
        type: "suggestion",
        subject: "",
        details: "",
        rating: "5",
      });
    } catch (err) {
      if (isRateLimitError(err)) return;
      toast.error("Submission failed", {
        description: "Failed to submit feedback. Please try again.",
      });
    } finally {
      setLoading(false);
    }
  };

  const allowSubmission =
    Boolean(feedbackFormData.type) &&
    Boolean(feedbackFormData.subject) &&
    Boolean(feedbackFormData.details);

  const ratings = [
    { value: "1", icon: <Angry className='h-6 w-6' />, label: "Poor" },
    { value: "2", icon: <FrownIcon className='h-6 w-6' />, label: "Not Good" },
    { value: "3", icon: <MehIcon className='h-6 w-6' />, label: "Okay" },
    { value: "4", icon: <SmileIcon className='h-6 w-6' />, label: "Good" },
    { value: "5", icon: <Laugh className='h-6 w-6' />, label: "Excellent" },
  ];

  return (
    <form onSubmit={handleFeedbackSubmit} className='space-y-5 pt-4'>
      {/* Name (optional) */}
      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='feedback-name'>Your name <span className='text-muted-foreground font-normal'>(optional)</span></Label>
        <Input
          id='feedback-name'
          placeholder='How should we address you?'
          value={feedbackFormData.name}
          onChange={(e) =>
            setFeedbackFormData({ ...feedbackFormData, name: e.target.value })
          }
        />
      </div>

      {/* Type */}
      <div className='flex flex-col gap-2.5'>
        <Label>Type of Feedback</Label>
        <Select
          value={feedbackFormData.type}
          onValueChange={(val) =>
            setFeedbackFormData({ ...feedbackFormData, type: val })
          }
        >
          <SelectTrigger>
            <SelectValue placeholder='Select feedback type' />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value='suggestion'>Suggestion</SelectItem>
            <SelectItem value='praise'>Praise</SelectItem>
            <SelectItem value='complaint'>Complaint</SelectItem>
            <SelectItem value='question'>Question</SelectItem>
            <SelectItem value='other'>Other</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Subject */}
      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='feedback-subject'>Subject</Label>
        <Input
          id='feedback-subject'
          placeholder='Brief subject of your feedback'
          value={feedbackFormData.subject}
          onChange={(e) =>
            setFeedbackFormData({
              ...feedbackFormData,
              subject: e.target.value,
            })
          }
          required
        />
      </div>

      {/* Details */}
      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='feedback-details'>Feedback Details</Label>
        <Textarea
          id='feedback-details'
          placeholder='Please provide detailed feedback'
          className='min-h-[150px]'
          value={feedbackFormData.details}
          onChange={(e) =>
            setFeedbackFormData({
              ...feedbackFormData,
              details: e.target.value,
            })
          }
          required
        />
      </div>

      {/* Rating */}
      <div className='flex flex-col gap-2.5'>
        <Label>How would you rate your experience?</Label>

        <RadioGroup
          value={feedbackFormData.rating}
          onValueChange={(val) =>
            setFeedbackFormData({ ...feedbackFormData, rating: val })
          }
          className='flex justify-between max-w-md mx-auto'
        >
          {ratings.map((r) => {
            const active = feedbackFormData.rating === r.value;
            return (
              <div key={r.value} className='flex flex-col items-center'>
                {/* keeps it accessible but clickable */}
                <RadioGroupItem
                  value={r.value}
                  id={`rating-${r.value}`}
                  className='sr-only'
                />

                <Label
                  htmlFor={`rating-${r.value}`}
                  className={clsx(
                    "cursor-pointer rounded-full p-3 transition-all duration-300 ease-in-out opacity-50 hover:-translate-y-1 flex flex-col items-center gap-1 select-none",
                    active && "!opacity-100",
                  )}
                >
                  {r.icon}
                  <span className='text-xs'>{r.label}</span>
                </Label>
              </div>
            );
          })}
        </RadioGroup>
      </div>

      <Button
        disabled={!allowSubmission || loading}
        type='submit'
        className='w-full'
      >
        {loading ? "Submitting..." : "Submit Feedback"}
      </Button>
    </form>
  );
}
