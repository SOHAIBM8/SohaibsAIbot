import { Routes, Route } from "react-router-dom";
import { LoginPage } from "@/auth/LoginPage";
import { RequireAuth } from "@/auth/RequireAuth";
import { DashboardPage } from "@/pages/DashboardPage";
import { LiveMarketPage } from "@/pages/LiveMarketPage";
import { PortfolioPage } from "@/pages/PortfolioPage";
import { OrdersPage } from "@/pages/OrdersPage";
import { OrderDetailPage } from "@/pages/OrderDetailPage";
import { PositionsPage } from "@/pages/PositionsPage";
import { RiskPage } from "@/pages/RiskPage";
import { ExperimentsPage } from "@/pages/ExperimentsPage";
import { AiAssistantPage } from "@/pages/AiAssistantPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { NotificationsPage } from "@/pages/NotificationsPage";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/market" element={<LiveMarketPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/orders" element={<OrdersPage />} />
        <Route path="/orders/:clientOrderId" element={<OrderDetailPage />} />
        <Route path="/positions" element={<PositionsPage />} />
        <Route path="/risk" element={<RiskPage />} />
        <Route path="/experiments" element={<ExperimentsPage />} />
        <Route path="/ai-assistant" element={<AiAssistantPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/notifications" element={<NotificationsPage />} />
      </Route>
    </Routes>
  );
}
