import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";

const NAV_ITEMS: { to: string; label: string }[] = [
  { to: "/", label: "Dashboard" },
  { to: "/market", label: "Live Market" },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/orders", label: "Orders" },
  { to: "/positions", label: "Positions" },
  { to: "/risk", label: "Risk" },
  { to: "/experiments", label: "Experiments" },
  { to: "/ai-assistant", label: "AI Assistant" },
  { to: "/settings", label: "Settings" },
  { to: "/notifications", label: "Notifications" },
];

export function Nav() {
  return (
    <nav className="flex flex-wrap gap-1 border-b border-gray-200 px-4 py-2 text-sm dark:border-gray-800">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/"}
          className={({ isActive }) =>
            cn(
              "rounded px-2.5 py-1.5 font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800",
              isActive && "bg-gray-900 text-white hover:bg-gray-900 dark:bg-gray-100 dark:text-gray-900",
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
