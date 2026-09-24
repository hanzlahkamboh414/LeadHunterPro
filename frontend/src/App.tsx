import { Navigate, Route, Routes } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import Dashboard from "./screens/Dashboard";
import Execute from "./screens/Execute";
import Leads from "./screens/Leads";
import Contacts from "./screens/Contacts";
import Campaigns from "./screens/Campaigns";
import EmailInbox from "./screens/EmailInbox";
import LeadDetail from "./screens/LeadDetail";
import History from "./screens/History";
import Settings from "./screens/Settings";
import Admin from "./screens/Admin";
import AdminGate from "./screens/AdminGate";
import Login from "./screens/Login";
import Signup from "./screens/Signup";
import Phones from "./screens/Phones";
import LinkedIn from "./screens/LinkedIn";
import ResetPassword from "./screens/ResetPassword";
import { PrivacyPolicy, TermsOfService } from "./screens/Legal";
import { useAuth } from "./contexts/AuthContext";
import { canSeeEmails, canSeePhones, isPhoneOnly } from "./lib/verticals";

// h-screen (a FIXED height), not min-h-screen: with a minimum the shell grew with
// its content, <main> never got a bounded height, and its overflow-y-auto did
// nothing — the window scrolled instead. That left the scroll container ambiguous
// and broke scroll restoration. Now the shell is exactly the viewport, the
// sidebar/topbar stay put, and <main> is the one real scroller. `min-h-0` on the
// flex children lets them shrink: flex items default to min-height:auto and will
// otherwise refuse to scroll.
export default function App() {
  const { user, token, loading, authEnabled } = useAuth();

  // While checking auth state, show nothing (prevents flash)
  if (loading) {
    return (
      <div className="h-screen w-full flex items-center justify-center bg-[#0B0E14]">
        <div className="text-slate-400 text-sm">Loading...</div>
      </div>
    );
  }

  // Not logged in — only show auth pages
  if (!token || !user) {
    const phonesVisible = canSeePhones(null, false, false); // open mode: shared account
    // Login auth is OFF (the admin toggle): the site opens straight into the
    // normal user UI. The admin panel is reachable ONLY through the secret
    // /admin4269 password gate.
    if (authEnabled === false) {
      return (
        <div className="workspace-shell h-screen w-full overflow-hidden bg-[#0B0E14] text-slate-200 flex font-sans">
          <a className="skip-link" href="#workspace-content">Skip to content</a>
          <Sidebar />
          <div className="flex-1 flex flex-col min-w-0 min-h-0">
            <Topbar />
            <main id="workspace-content" tabIndex={-1} className="workspace-main flex-1 min-h-0 overflow-y-auto">
              <Routes>
                <Route path="/admin4269" element={<AdminGate />} />
                <Route path="/" element={<Dashboard />} />
                <Route path="/research" element={<Execute />} />
                {phonesVisible && <Route path="/phones" element={<Phones />} />}
                {/* P4: the LinkedIn byproduct lane — every account, no gate. */}
                <Route path="/linkedin" element={<LinkedIn />} />
                <Route path="/leads" element={<Leads />} />
                <Route path="/contacts" element={<Contacts />} />
                <Route path="/campaigns" element={<Campaigns />} />
                {/* Phase E7: the connected Gmail's mail inside the app. */}
                <Route path="/email" element={<EmailInbox />} />
                <Route path="/leads/:email" element={<LeadDetail />} />
                <Route path="/history" element={<History />} />
                <Route path="/settings" element={<Settings />} />
                {/* The login pages are OFF in open mode — they just bounce
                    back into the app. */}
                <Route path="/login" element={<Navigate to="/" replace />} />
                <Route path="/signup" element={<Navigate to="/" replace />} />
                <Route path="/reset-password" element={<Navigate to="/" replace />} />
                {/* Public legal pages (E6 Google verification — reachable
                    without a session, from any branch). */}
                <Route path="/privacy" element={<PrivacyPolicy />} />
                <Route path="/terms" element={<TermsOfService />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </main>
          </div>
        </div>
      );
    }

    return (
      <Routes>
        <Route path="/admin4269" element={<AdminGate />} />
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="/reset-password" element={<ResetPassword />} />
        {/* Public legal pages (E6 Google verification). */}
        <Route path="/privacy" element={<PrivacyPolicy />} />
        <Route path="/terms" element={<TermsOfService />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  // Logged in — normal app shell
  // P3: the Phones route exists only for accounts whose category includes
  // phones (phones | both) or the admin — the nav link uses the same rule.
  const phonesVisible = canSeePhones(user.category, user.is_admin, true);
  const emailsVisible = canSeeEmails(user.category, user.is_admin, true);
  const phoneOnly = isPhoneOnly(user.category, user.is_admin, true);
  return (
    <div className="workspace-shell h-screen w-full overflow-hidden bg-[#0B0E14] text-slate-200 flex font-sans">
      <a className="skip-link" href="#workspace-content">Skip to content</a>
          <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <Topbar />
        <main id="workspace-content" tabIndex={-1} className="workspace-main flex-1 min-h-0 overflow-y-auto">
          <Routes>
            <Route path="/" element={phoneOnly ? <Navigate to="/phones" replace /> : <Dashboard />} />
            {emailsVisible && <Route path="/research" element={<Execute />} />}
            {phonesVisible && <Route path="/phones" element={<Phones />} />}
            {/* P4: the LinkedIn byproduct lane — every authenticated account,
                no signup-category gate (matches the backend's user gate). */}
            {emailsVisible && <Route path="/linkedin" element={<LinkedIn />} />}
            {emailsVisible && <Route path="/leads" element={<Leads />} />}
            {emailsVisible && <Route path="/contacts" element={<Contacts />} />}
            {emailsVisible && <Route path="/campaigns" element={<Campaigns />} />}
            {/* Phase E7: the connected Gmail's mail inside the app. */}
            {emailsVisible && <Route path="/email" element={<EmailInbox />} />}
            {emailsVisible && <Route path="/leads/:email" element={<LeadDetail />} />}
            {emailsVisible && <Route path="/history" element={<History />} />}
            {emailsVisible && <Route path="/settings" element={<Settings />} />}
            {/* The secret gate doubles as an admin-login shortcut for a
                session that's already admin. */}
            <Route path="/admin4269" element={<Navigate to="/admin" replace />} />
            {user.is_admin && <Route path="/admin" element={<Admin />} />}
            {/* Public legal pages (E6 Google verification). */}
            <Route path="/privacy" element={<PrivacyPolicy />} />
            <Route path="/terms" element={<TermsOfService />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
