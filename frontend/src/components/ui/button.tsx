import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-400 disabled:pointer-events-none disabled:opacity-50 cursor-pointer",
  {
    variants: {
      variant: {
        default: "bg-zinc-50 text-zinc-950 hover:bg-zinc-200 shadow-xs",
        destructive: "bg-rose-600 text-zinc-50 hover:bg-rose-500",
        outline: "border border-zinc-800 bg-card-elevated/70 text-zinc-200 hover:border-zinc-700 hover:bg-zinc-800/80 hover:text-zinc-50",
        secondary: "bg-zinc-800 text-zinc-100 hover:bg-zinc-700",
        ghost: "text-zinc-400 hover:bg-zinc-800/70 hover:text-zinc-100",
        link: "text-zinc-200 underline-offset-4 hover:underline",
      },
      size: {
        default: "h-8 px-3 py-1.5",
        sm: "h-7 rounded-md px-2.5 text-[11px]",
        lg: "h-9 rounded-lg px-4 text-sm",
        icon: "h-8 w-8",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  }
);
Button.displayName = "Button";
