import { Navigate, Route, Routes } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import Dashboard from "./screens/Dashboard";
import Execute from "./screens/Execute";
import Leads from "./screens/Leads";
import Contacts from "./screens/Contacts";
import LeadDetail from "./screens/LeadDetail";
import History from "./screens/History";
import Settings from "./screens/Settings";
import Admin from "./screens/Admin";

// h-screen (a FIXED height), not min-h-screen: with a minimum the shell grew with
// its content, <main> never got a bounded height, and its overflow-y-auto did
// nothing — the window scrolled instead. That left the scroll container ambiguous
// and broke scroll restoration. Now the shell is exactly the viewport, the
// sidebar/topbar stay put, and <main> is the one real scroller. `min-h-0` on the
// flex children lets them shrink: flex items default to min-height:auto and will
// otherwise refuse to scroll.
export default function App() {
  return (
    <div className="h-screen w-full overflow-hidden bg-[#0B0E14] text-slate-200 flex font-sans">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <Topbar />
        <main className="flex-1 min-h-0 overflow-y-auto">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/research" element={<Execute />} />
            <Route path="/leads" element={<Leads />} />
            <Route path="/contacts" element={<Contacts />} />
            <Route path="/leads/:email" element={<LeadDetail />} />
            <Route path="/history" element={<History />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/admin" element={<Admin />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
