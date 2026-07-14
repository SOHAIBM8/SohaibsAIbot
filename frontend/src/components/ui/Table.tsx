import type { ReactNode } from "react";

/**
 * A wide table scrolls inside its own horizontal container — the page
 * body itself never scrolls sideways.
 */
export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-max text-left text-sm">{children}</table>
    </div>
  );
}

export function Thead({ children }: { children: ReactNode }) {
  return (
    <thead className="border-b border-gray-200 text-xs uppercase text-gray-500 dark:border-gray-800 dark:text-gray-400">
      {children}
    </thead>
  );
}

export function Tbody({ children }: { children: ReactNode }) {
  return <tbody className="divide-y divide-gray-100 dark:divide-gray-800">{children}</tbody>;
}

export function Th({ children }: { children: ReactNode }) {
  return <th className="whitespace-nowrap px-3 py-2 font-medium">{children}</th>;
}

export function Td({ children }: { children: ReactNode }) {
  return <td className="whitespace-nowrap px-3 py-2">{children}</td>;
}
