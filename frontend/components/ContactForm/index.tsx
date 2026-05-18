import React from "react";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { BugIcon, MessageSquareIcon, SendIcon } from "lucide-react";
import FeedbackForm from "./FeedbackForm";
import ContactUs from "./ContactUs";
import BugReport from "./BugReport";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { GuidedTooltip } from "@/components/ui/guided-tooltip";

export default function FeedbackModal() {
  return (
    <Dialog>
      <GuidedTooltip targetId='feedback-button' side='left' wrapperClassName='contents'>
        <DialogTrigger asChild>
          <span className='fixed transition-transform ease-in-out duration-300 bottom-1/3 right-0 translate-x-1/2 hover:translate-x-0 bg-card p-3 rounded-full flex items-center justify-center !z-[100] shadow-xl cursor-pointer border border-border'>
            <BugIcon strokeWidth={1.4} className='size-7 text-foreground' />
          </span>
        </DialogTrigger>
      </GuidedTooltip>

      <DialogContent className='max-w-3xl w-full max-h-[90vh] border-0 overflow-y-auto'>
        <div className='w-full flex flex-col gap-5'>
          <DialogHeader className='items-start'>
            <DialogTitle className='text-2xl font-semibold'>
              Help us improve
            </DialogTitle>
            <DialogDescription className='text-xs opacity-70'>
              We value your input. Please use the appropriate form to share your
              thoughts with us.
            </DialogDescription>
          </DialogHeader>

          <div>
            <Tabs defaultValue='bug' className='w-full'>
              <TabsList className='grid w-full grid-cols-3 bg-muted p-1 rounded-md'>
                <TabsTrigger value='bug'>
                  <BugIcon className='h-4 w-4 me-2' />
                  <span className='hidden sm:inline'>Bug Report</span>
                  <span className='sm:hidden'>Bug</span>
                </TabsTrigger>

                <TabsTrigger value='feedback'>
                  <MessageSquareIcon className='h-4 w-4 me-2' />
                  <span>Feedback</span>
                </TabsTrigger>

                <TabsTrigger value='contact'>
                  <SendIcon className='h-4 w-4 me-2' />
                  <span>Contact Us</span>
                </TabsTrigger>
              </TabsList>

              {/* Bug Report Form */}
              <TabsContent value='bug' className='mt-4'>
                <BugReport />
              </TabsContent>

              {/* Feedback Form */}
              <TabsContent value='feedback' className='mt-4'>
                <FeedbackForm />
              </TabsContent>

              {/* Contact Us Form */}
              <TabsContent value='contact' className='mt-4'>
                <ContactUs />
              </TabsContent>
            </Tabs>
          </div>

        </div>
      </DialogContent>
    </Dialog>
  );
}
