import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[11px] font-medium transition-colors focus:outline-none",
  {
    variants: {
      variant: {
        default: "border-zinc-700 bg-zinc-800/80 text-zinc-100",
        secondary: "border-zinc-800 bg-zinc-900 text-zinc-300",
        destructive: "border-rose-800/70 bg-rose-950/60 text-rose-300",
        outline: "border-zinc-700 text-zinc-300",
        success: "border-emerald-700/70 bg-emerald-950/60 text-emerald-300",
        warning: "border-amber-800/70 bg-amber-950/50 text-amber-300",
        info: "border-sky-800/70 bg-sky-950/60 text-sky-300",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
