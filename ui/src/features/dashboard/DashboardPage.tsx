import { PageHeader } from "@/components/common/PageHeader";
import { useAuth } from "@/features/auth/use-auth";
import { AdminDashboard } from "@/features/dashboard/AdminDashboard";
import { MemberDashboard } from "@/features/dashboard/MemberDashboard";

/** Landing overview. Platform admins get the whole platform; everyone else
 * sees only the teams they belong to. */
export function DashboardPage() {
  const { user } = useAuth();
  return (
    <>
      <PageHeader
        command="status"
        title="Dashboard"
        description={
          user?.is_admin
            ? "Platform at a glance — tenancy, catalog, gateway readiness, spend, savings and activity."
            : "Your teams at a glance — savings, and where your roles can take you."
        }
      />
      {user?.is_admin ? <AdminDashboard /> : <MemberDashboard />}
    </>
  );
}
