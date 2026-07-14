import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "danger";

const VARIANT_CLASSES: Record<Variant, string> = {
  primary: "bg-blue-600 text-white hover:bg-blue-700 disabled:bg-blue-300",
  secondary:
    "bg-white text-gray-700 border border-gray-300 hover:bg-gray-50 disabled:text-gray-400 " +
    "dark:bg-gray-900 dark:text-gray-200 dark:border-gray-700 dark:hover:bg-gray-800",
  danger: "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}

export function Button({ variant = "secondary", className, disabled, ...props }: ButtonProps) {
  return (
    <button
      disabled={disabled}
      className={cn(
        "rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed",
        VARIANT_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}
