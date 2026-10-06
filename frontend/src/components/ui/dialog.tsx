import type * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"
import { cn } from "@/lib/utils"

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
function DialogContent({ className, children, ...props }: DialogPrimitive.DialogContentProps) {
  return <DialogPrimitive.Portal><DialogPrimitive.Overlay className="dialog-overlay" /><DialogPrimitive.Content className={cn("dialog-content", className)} {...props}>
    {children}<DialogPrimitive.Close className="dialog-close" aria-label="Close"><X size={18} /></DialogPrimitive.Close>
  </DialogPrimitive.Content></DialogPrimitive.Portal>
}
function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) { return <div className={cn("dialog-header", className)} {...props} /> }
function DialogTitle(props: DialogPrimitive.DialogTitleProps) { return <DialogPrimitive.Title className="text-lg font-semibold" {...props} /> }
function DialogDescription(props: DialogPrimitive.DialogDescriptionProps) { return <DialogPrimitive.Description className="text-sm text-muted-foreground" {...props} /> }
export { Dialog, DialogTrigger, DialogContent, DialogHeader, DialogTitle, DialogDescription }
