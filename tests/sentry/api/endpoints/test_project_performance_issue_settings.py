from django.urls import reverse
from rest_framework.exceptions import ErrorDetail

from sentry.testutils import APITestCase

PERFORMANCE_ISSUE_FEATURES = {
    "organizations:performance-view": True,
    "organizations:performance-issues": True,
}


class ProjectPerformanceIssueSettingsTest(APITestCase):
    endpoint = "sentry-api-0-project-performance-issue-settings"

    def setUp(self) -> None:
        super().setUp()

        self.login_as(user=self.user)
        self.project = self.create_project()

        self.url = reverse(
            self.endpoint,
            kwargs={
                "organization_slug": self.project.organization.slug,
                "project_slug": self.project.slug,
            },
        )

    def test_get_returns_default(self):
        with self.feature(PERFORMANCE_ISSUE_FEATURES):
            response = self.client.get(self.url, format="json")

        assert response.status_code == 200, response.content
        assert response.data["n_plus_one_db_count"] == 5
        assert response.data["n_plus_one_db_duration_threshold"] == 500

    def test_get_returns_error_without_feature_enabled(self):
        with self.feature({}):
            response = self.client.get(self.url, format="json")
            assert response.status_code == 404

    def test_update_project_setting(self):
        with self.feature(PERFORMANCE_ISSUE_FEATURES):
            response = self.client.put(
                self.url,
                data={
                    "n_plus_one_db_count": 17,
                },
            )

        assert response.status_code == 200, response.content
        assert response.data["n_plus_one_db_count"] == 17

        with self.feature(PERFORMANCE_ISSUE_FEATURES):
            get_response = self.client.get(self.url, format="json")

        assert get_response.status_code == 200, response.content
        assert get_response.data["n_plus_one_db_count"] == 17
        assert get_response.data["n_plus_one_db_duration_threshold"] == 500

    def test_update_project_setting_check_validation(self):
        with self.feature(PERFORMANCE_ISSUE_FEATURES):
            response = self.client.put(
                self.url,
                data={
                    "n_plus_one_db_count": -1,
                },
            )

        assert response.status_code == 400, response.content
        assert response.data == {
            "n_plus_one_db_count": [
                ErrorDetail(
                    string="Ensure this value is greater than or equal to 0.", code="min_value"
                )
            ]
        }
