"use client";

import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { submitContactUs } from "@/app/api/contact";
import { useSessionStore } from "@/store/session";
import { toast } from "sonner";
import { isRateLimitError } from "@/lib/api-client";

export default function ContactUs() {
  const userId = useSessionStore((s) => s.userId);
  const [contactFormData, setContactFormData] = useState({
    firstName: "",
    lastName: "",
    email: "",
    subject: "",
    message: "",
  });

  const [loading, setLoading] = useState(false);

  const handleContactSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await submitContactUs(userId, contactFormData);
      toast.success("Message sent", {
        description: "We'll get back to you as soon as possible.",
      });
      setContactFormData({
        firstName: "",
        lastName: "",
        email: "",
        subject: "",
        message: "",
      });
    } catch (err) {
      if (isRateLimitError(err)) return;
      toast.error("Failed to send", {
        description: "Failed to send message. Please try again.",
      });
    } finally {
      setLoading(false);
    }
  };

  const allowSubmission =
    Boolean(contactFormData.firstName) &&
    Boolean(contactFormData.lastName) &&
    Boolean(contactFormData.email) &&
    Boolean(contactFormData.subject) &&
    Boolean(contactFormData.message);

  return (
    <form onSubmit={handleContactSubmit} className='space-y-5 pt-4'>
      <div className='grid grid-cols-1 sm:grid-cols-2 gap-4'>
        <div className='flex flex-col gap-2.5'>
          <Label htmlFor='contact-first-name'>First Name</Label>
          <Input
            id='contact-first-name'
            placeholder='First name'
            value={contactFormData.firstName}
            onChange={(e) =>
              setContactFormData({
                ...contactFormData,
                firstName: e.target.value,
              })
            }
            required
          />
        </div>

        <div className='flex flex-col gap-2.5'>
          <Label htmlFor='contact-last-name'>Last Name</Label>
          <Input
            id='contact-last-name'
            placeholder='Last name'
            value={contactFormData.lastName}
            onChange={(e) =>
              setContactFormData({
                ...contactFormData,
                lastName: e.target.value,
              })
            }
            required
          />
        </div>
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='contact-email'>Email</Label>
        <Input
          id='contact-email'
          type='email'
          placeholder='Your email address'
          value={contactFormData.email}
          onChange={(e) =>
            setContactFormData({
              ...contactFormData,
              email: e.target.value,
            })
          }
          required
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='contact-subject'>Subject</Label>
        <Input
          id='contact-subject'
          placeholder='Subject of your message'
          value={contactFormData.subject}
          onChange={(e) =>
            setContactFormData({
              ...contactFormData,
              subject: e.target.value,
            })
          }
          required
        />
      </div>

      <div className='flex flex-col gap-2.5'>
        <Label htmlFor='contact-message'>Message</Label>
        <Textarea
          id='contact-message'
          placeholder='How can we help you?'
          className='min-h-[150px]'
          value={contactFormData.message}
          onChange={(e) =>
            setContactFormData({
              ...contactFormData,
              message: e.target.value,
            })
          }
          required
        />
      </div>

      <Button disabled={!allowSubmission || loading} type='submit' className='w-full'>
        {loading ? "Sending..." : "Send Message"}
      </Button>
    </form>
  );
}
