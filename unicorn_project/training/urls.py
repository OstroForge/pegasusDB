# unicorn_project/training/urls.py
from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views

from . import views
from . import views_admin
from . import views_admin as app_admin
from . import views_engineer as engv
from . import views_inspector as inspv
from . import views_instructor as instv
from . import views_instructor
from . import views_course_swaps as swapv
from . import views_emergency_takeover as takeoverv
from . import views_inbox as inboxv
from . import views_public
from . import views_telegram

from .views_public import (
    delegate_exam_start,      # start (enter details / match register)
    delegate_exam_rules,      # rules screen
    delegate_exam_run,        # run the exam (Q&A screen)
    delegate_exam_review,     # review answers before submit
    delegate_exam_finish,     # results
)

from . import views_certificates

from .views_admin import (
    site_settings,
    meta_settings_edit,
    logo_override_edit,
    logo_override_delete,
)


urlpatterns = [
    # ---------- Core / Home ----------
    path("health/", views.health_check, name="health_check"),
    path("telegram/webhook/<str:secret>/", views_telegram.telegram_webhook, name="telegram_webhook"),
    path("app/", views.app_entry, name="app_entry"),
    path("", views.home, name="home"),
    path("switch-role/<str:role>/", views.switch_role, name="switch_role"),

    # Post-login routers
    # path("app/after-login/", instv.post_login, name="post_login"),
    path("post-login/", views_instructor.post_login, name="post_login"),

    # ---------- Instructor area ----------
    # Landing page -> My bookings
    path("app/instructor/", instv.instructor_dashboard, name="instructor_dashboard"),
    path("app/instructor/bookings/", instv.instructor_bookings, name="instructor_bookings"),
    path("app/instructor/course-swaps/", swapv.instructor_course_swaps, name="instructor_course_swaps"),
    path("app/instructor/course-swaps/request/", swapv.instructor_course_swap_request, name="instructor_course_swap_request"),
    path("app/instructor/course-swaps/<uuid:pk>/accept/", swapv.instructor_course_swap_accept, name="instructor_course_swap_accept"),
    path("app/instructor/course-swaps/<uuid:pk>/decline/", swapv.instructor_course_swap_decline, name="instructor_course_swap_decline"),
    path("app/instructor/course-swaps/<uuid:pk>/cancel/", swapv.instructor_course_swap_cancel, name="instructor_course_swap_cancel"),
    path("app/instructor/emergency-takeover/", takeoverv.instructor_emergency_takeover, name="instructor_emergency_takeover"),
    path("app/instructor/emergency-takeover/<uuid:pk>/confirm/", takeoverv.instructor_emergency_takeover_confirm, name="instructor_emergency_takeover_confirm"),
    path("app/instructor/practice-bookings/new/<uuid:business_id>/", instv.instructor_dummy_booking_new, name="instructor_dummy_booking_new"),
    path("app/instructor/booking/<uuid:pk>/", instv.instructor_booking_detail, name="instructor_booking_detail"),
    path("app/instructor/booking/<uuid:pk>/delete-dummy/", instv.instructor_delete_dummy_booking, name="instructor_delete_dummy_booking"),
    path("app/instructor/booking/<uuid:pk>/telegram-send/", instv.instructor_send_telegram_booking, name="instructor_booking_telegram_send"),
    path("app/instructor/booking/<uuid:pk>/email-send/", instv.instructor_send_email_booking, name="instructor_booking_email_send"),
    path("app/instructor/booking/<uuid:pk>/course-pack.pdf", instv.instructor_booking_course_pack_pdf, name="instructor_booking_course_pack_pdf"),
    path("app/instructor/booking/<uuid:booking_id>/accident-reports/poll/", views.instructor_booking_accident_reports_poll, name="instructor_booking_accident_reports_poll"),
    path("app/instructor/booking/<uuid:booking_id>/accident-reports/export-pptx/", views.instructor_booking_accident_reports_export, name="instructor_booking_accident_reports_export"),
    path("app/instructor/booking/<uuid:booking_id>/accident-reports/delete/", views.instructor_booking_accident_reports_delete, name="instructor_booking_accident_reports_delete"),
    path("app/instructor/booking/<uuid:booking_id>/accident-reports/anonymise/", views.instructor_booking_accident_reports_anonymise, name="instructor_booking_accident_reports_anonymise"),
    path("app/instructor/day/<int:pk>/registers/", instv.instructor_day_registers, name="instructor_day_registers"),
    path("app/instructor/day/<int:pk>/registers/copy-previous/", instv.instructor_copy_previous_day_registers, name="instructor_copy_previous_day_registers"),
    path("app/instructor/register/<int:pk>/edit/", instv.instructor_delegate_edit, name="instructor_delegate_edit"),
    path("app/instructor/day/<int:day_pk>/registers/new/", instv.instructor_delegate_new, name="instructor_delegate_new"),
    path("app/profile/", views.user_profile, name="user_profile"),
    path("app/profile/request-course/", views.instructor_request_course_delivery, name="instructor_request_course_delivery"),
    path("app/telegram/link-token/", views_telegram.telegram_link_token, name="telegram_link_token"),
    path("app/telegram/unlink/", views_telegram.telegram_unlink, name="telegram_unlink"),
    path("app/preferences/night-mode/", views.toggle_night_mode, name="toggle_night_mode"),
    path("app/instructor/day/<int:pk>/registers/send-pdf/", views_instructor.instructor_day_registers_pdf, name="instructor_send_register_pdf"),
    path("app/instructor/day/<int:pk>/registers/poll/", views_instructor.instructor_day_registers_poll, name="instructor_day_registers_poll"),
    path("app/instructor/booking/<uuid:pk>/upload-receipt/", instv.instructor_upload_receipt, name="instructor_upload_receipt"),
    path("app/instructor/booking/<uuid:pk>/receipts/", instv.instructor_list_receipts, name="instructor_list_receipts",),
    path("app/instructor/booking/<uuid:pk>/delete-receipt/", instv.instructor_delete_receipt, name="instructor_delete_receipt",),
    path("instructor/booking/<uuid:pk>/fee/", views_instructor.booking_fee, name="booking-fee"),
    path("instructor/booking/<uuid:pk>/course-summary.pdf", views_instructor.instructor_course_summary_pdf, name="instructor_course_summary_pdf"),
    path("instructor/booking/ref/<slug:ref>/course-summary.pdf", views_instructor.instructor_course_summary_by_ref_pdf, name="instructor_course_summary_by_ref_pdf"),
    path("instructor/booking/<uuid:pk>/certificates/", instv.instructor_booking_certificates_pdf, name="instructor_booking_certificates"),
    path("app/instructor/home/", instv.instructor_dashboard, name="instructor_home"),


    
    # Instructor: delete a delegate row
    path("app/instructor/register/<int:pk>/delete/", instv.instructor_delegate_delete, name="instructor_delegate_delete"),

    # Instructor: export day's register to PDF
    path("app/instructor/day/<int:pk>/registers/pdf/", instv.instructor_day_registers_pdf, name="instructor_day_registers_pdf"),

    # ---------- Public delegate register ----------
    path("r/<str:code>/qr.png", views.public_register_short_qr, name="public_register_short_qr"),
    path("r/<str:code>/", views.public_register_short, name="public_register_short"),
    path("register/", views.public_delegate_register, name="public_delegate_register"),
    path("register/success/", views.public_delegate_register_success, name="public_delegate_register_success"),
    path("register/instructors/", views.public_delegate_instructors_api, name="public_delegate_instructors_api"),
    path("register/sessions/", views.public_delegate_sessions_api, name="public_delegate_sessions_api"),

    # ---------- Public/API helpers ----------
    path("public/attendance/<int:booking_day_id>/", views.public_attendance, name="public_attendance"),
    path("api/locations/", views.api_locations_by_business, name="api_locations_by_business"),

    # ---------- Admin dashboard ----------
    path("app/admin/", views_admin.admin_dashboard, name="app_admin_dashboard"),
    path("app/admin/exams/<int:pk>/", app_admin.exam_form, name="admin_exam_edit"),
    path("app/admin/exam-images/<str:filename>", app_admin.exam_library_image, name="admin_exam_library_image"),
    path("delegates/search/", views_admin.admin_delegate_search, name="admin_delegate_search"),
    path("app-admin/dashboard/", views_admin.admin_dashboard, name="admin_dashboard"),
    path("app-admin/api/courses-today/", views_admin.api_courses_today, name="api_courses_today"),
    path("app-admin/api/courses-awaiting-closure/", views_admin.api_courses_awaiting_closure, name="api_courses_awaiting_closure"),
    path("app-admin/api/courses-in-7-days/", views_admin.api_courses_in_7_days, name="api_courses_in_7_days"),
    path("api/outstanding-invoices/", views_admin.api_outstanding_invoices, name="api_outstanding_invoices"),
    path("app-admin/api/inbox-open/", inboxv.api_inbox_open, name="api_admin_inbox_open"),
    path("app/api/inbox-unread/", inboxv.api_inbox_unread, name="api_inbox_unread"),
    path("app/api/inbox-stream/", inboxv.inbox_stream, name="api_inbox_stream"),

    path("app/inbox/", inboxv.inbox_list, name="inbox_list"),
    path("app/inbox/<uuid:pk>/course-request/", inboxv.inbox_course_request_action, name="inbox_course_request_action"),
    path("app/inbox/<uuid:pk>/swap-accept/", inboxv.inbox_swap_accept, name="inbox_swap_accept"),
    path("app/inbox/<uuid:pk>/swap-decline/", inboxv.inbox_swap_decline, name="inbox_swap_decline"),
    path("app/admin/inbox/", inboxv.inbox_list, name="admin_inbox_list"),
    path("app/admin/inbox/<uuid:pk>/course-request/", inboxv.inbox_course_request_action, name="admin_inbox_course_request_action"),

    # Businesses
    path("app/admin/businesses/", app_admin.business_list, name="admin_business_list"),
    path("app/admin/businesses/new/", app_admin.business_form, name="admin_business_new"),
    path("app/admin/businesses/<uuid:pk>/", app_admin.business_form, name="admin_business_edit"),
    path("app/admin/businesses/<uuid:pk>/delete/", app_admin.business_delete, name="admin_business_delete"),

    # Training Locations
    path("app/admin/businesses/<uuid:business_id>/locations/new/", app_admin.location_new, name="admin_location_new"),
    path("app/admin/locations/create-ajax/", app_admin.admin_location_create_ajax, name="admin_location_create_ajax"),
    path("app/admin/locations/<uuid:pk>/update-ajax/", app_admin.admin_location_update_ajax, name="admin_location_update_ajax"),
    path("app/admin/locations/<uuid:pk>/delete-ajax/", app_admin.admin_location_delete_ajax, name="admin_location_delete_ajax"),
    path("app/admin/locations/<uuid:pk>/unarchive-ajax/", app_admin.admin_location_unarchive_ajax, name="admin_location_unarchive_ajax"),
    path("app/admin/locations/<uuid:pk>/", app_admin.location_edit, name="admin_location_edit"),
    path("app/admin/locations/<uuid:pk>/unarchive/", app_admin.location_unarchive, name="admin_location_unarchive"),
    path("app/admin/locations/<uuid:pk>/delete/", app_admin.location_delete, name="admin_location_delete"),


    # Course Types
    path("app/admin/courses/", app_admin.course_list, name="admin_course_list"),
    path("app/admin/courses/new/", app_admin.course_form, name="admin_course_new"),
    path("app/admin/courses/preview-code/", app_admin.course_code_preview, name="admin_course_code_preview"),
    path("app/admin/courses/<uuid:pk>/", app_admin.course_form, name="admin_course_edit"),
    path("app/admin/courses/<uuid:pk>/delete/", app_admin.course_delete, name="admin_course_delete"),
    path("app/admin/course-types/", app_admin.course_list, name="admin_course_type_list"),  # alias

    # Instructors (admin)
    path("app/admin/personnel/", app_admin.admin_personnel_list, name="admin_personnel_list"),
    path("app/admin/personnel/new/", app_admin.admin_personnel_new, name="admin_personnel_new"),
    path("app/admin/personnel/<uuid:pk>/", app_admin.admin_personnel_edit, name="admin_personnel_edit"),
    path("app/admin/personnel/<uuid:pk>/delete/", app_admin.admin_personnel_delete, name="admin_personnel_delete"),
    path("app/instructor/booking/<uuid:pk>/invoice/preview/", views_instructor.invoice_preview, name="instructor_invoice_preview"),
    path("accident-reports/", views.accident_report_list, name="accident_report_list"),
    path("app/admin/personnel/<uuid:pk>/resend-password/", app_admin.admin_personnel_resend_password, name="admin_personnel_resend_password"),
    path("app/instructor/booking/<uuid:booking_id>/ics/", views_instructor.download_booking_ics, name="download_booking_ics"),
    path("app/instructor/resources/", instv.instructor_resources, name="instructor_resources"),
    path("app/instructor/calendar/", instv.instructor_calendar, name="instructor_calendar"),
    path("app/instructor/calendar/events/", instv.instructor_calendar_events, name="instructor_calendar_events"),
    path("app/instructor/calendar/availability/", instv.instructor_set_availability, name="instructor_set_availability"),
    path("app/instructor/calendar/patterns/", instv.instructor_list_patterns, name="instructor_list_patterns"),
    path("app/instructor/calendar/patterns/create/", instv.instructor_create_pattern, name="instructor_create_pattern"),
    path("app/instructor/calendar/patterns/update/", instv.instructor_update_pattern, name="instructor_update_pattern"),
    path("app/instructor/calendar/patterns/delete/", instv.instructor_delete_pattern, name="instructor_delete_pattern"),



    # Bookings (admin)
    path("app/admin/bookings/", app_admin.booking_list, name="admin_booking_list"),
    path("app/admin/bookings/new/", app_admin.booking_form, name="admin_booking_new"),
    path("app/admin/bookings/<uuid:pk>/", app_admin.booking_form, name="admin_booking_edit"),
    path("admin/bookings/<uuid:pk>/invoice-pdf/", views_admin.admin_invoice_pdf, name="admin_invoice_pdf"),
    path("app/admin/bookings/<uuid:pk>/certificates/", app_admin.admin_booking_certificates_selected, name="admin_booking_certificates_selected"),
    path("app/admin/registers/<int:reg_pk>/certificate-name/", app_admin.admin_certificate_name_edit, name="admin_certificate_name_edit"),


    path("app/admin/bookings/<uuid:pk>/delete/", app_admin.booking_delete, name="admin_booking_delete"),
    path("app/admin/bookings/<uuid:pk>/cancel/", app_admin.booking_cancel, name="admin_booking_cancel"),
    path("app/admin/bookings/<uuid:pk>/reinstate/", app_admin.booking_reinstate, name="admin_booking_reinstate"),
    path("app/admin/bookings/<uuid:pk>/telegram-send/", app_admin.admin_booking_telegram_send, name="admin_booking_telegram_send"),
    path("app/admin/bookings/<uuid:pk>/email-send/", app_admin.admin_booking_email_send, name="admin_booking_email_send"),
    path("app/admin/bookings/<uuid:pk>/course-pack.pdf", app_admin.admin_booking_course_pack_pdf, name="admin_booking_course_pack_pdf"),
    path("app/admin/bookings/<uuid:booking_id>/accident-reports/poll/", views.admin_booking_accident_reports_poll, name="admin_booking_accident_reports_poll"),
    path("app/admin/bookings/<uuid:booking_id>/accident-reports/export-pptx/", views.admin_booking_accident_reports_export, name="admin_booking_accident_reports_export"),
    path("app/admin/bookings/<uuid:booking_id>/accident-reports/delete/", views.admin_booking_accident_reports_delete, name="admin_booking_accident_reports_delete"),
    path("app/admin/bookings/<uuid:booking_id>/accident-reports/anonymise/", views.admin_booking_accident_reports_anonymise, name="admin_booking_accident_reports_anonymise"),
    path("app/admin/bookings/<uuid:pk>/unlock/", app_admin.booking_unlock, name="admin_booking_unlock"),
    path('admin/invoice/<uuid:pk>/pdf/', views_admin.admin_invoice_pdf, name='admin_invoice_pdf'),


    # Users (admin) – ints
    path("app/admin/users/", app_admin.admin_user_list, name="admin_user_list"),
    path("app/admin/users/new/", app_admin.admin_user_new, name="admin_user_new"),
    path("app/admin/users/<int:pk>/", app_admin.admin_user_edit, name="admin_user_edit"),
    path("app/admin/users/<int:pk>/impersonate/", app_admin.admin_impersonate_user, name="admin_impersonate_user"),
    path("app/impersonation/stop/", app_admin.admin_stop_impersonation, name="admin_stop_impersonation"),

    # Registers (admin)
    path("app/admin/booking-days/<int:pk>/registers/", app_admin.booking_day_registers, name="admin_booking_day_registers"),
    path("app/admin/registers/<int:pk>/delete/", app_admin.delegate_register_delete, name="admin_register_delete"),

    # ---------- Debug ----------
    path("debug/whoami/", instv.whoami, name="debug_whoami"),

    # ---------- Auth ----------
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),

    path(
        "accounts/logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),

    path(
        "accounts/password_change/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change_form.html",
            success_url="/accounts/password_change/done/",
        ),
        name="password_change",
    ),

    path(
        "accounts/password_change/done/",
        views_admin.password_change_done_and_clear,
        name="password_change_done",
    ),


    # ---------- Assessments ----------
    # (matrix is embedded in booking detail; these are save/export endpoints)
    path("app/instructor/booking/<uuid:pk>/assessment/save/", instv.instructor_assessment_save, name="instructor_assessment_save"),
    path("app/instructor/booking/<uuid:pk>/assessment/pdf/", instv.instructor_assessment_pdf, name="instructor_assessment_pdf"),
    path("app/instructor/booking/<uuid:pk>/assessments/autosave/", views_instructor.instructor_assessment_autosave,name="instructor_assessment_autosave"),
    path("app/instructor/booking/<uuid:pk>/assessments/outcome/", views_instructor.instructor_assessment_outcome_autosave, name="instructor_assessment_outcome_autosave"),
    path("app/instructor/booking/<uuid:pk>/assessments/optional-modules/", views_instructor.instructor_assessment_optional_modules_save, name="instructor_assessment_optional_modules_save"),

    # ---------- Public Feedback (QR-driven form) ----------
    path("f/<str:code>/qr.png", views.public_feedback_short_qr, name="public_feedback_short_qr"),
    path("f/<str:code>/", views.public_feedback_short, name="public_feedback_short"),
    path("e/<str:code>/qr.png", views.public_exam_short_qr, name="public_exam_short_qr"),
    path("e/<str:code>/", views.public_exam_short, name="public_exam_short"),
    path("ar/<str:code>/qr.png", views.public_accident_report_short_qr, name="public_accident_report_short_qr"),
    path("ar/<str:code>/", views.public_accident_report_short, name="public_accident_report_short"),
    path("feedback/", views.public_feedback_form, name="public_feedback_form"),
    path("feedback/thanks/", views.public_feedback_thanks, name="public_feedback_thanks"),
    path("feedback/<uuid:pk>/pdf/", views.public_feedback_pdf, name="public_feedback_pdf"),
    path("feedback/instructors", views.public_feedback_instructors_api, name="public_feedback_instructors_api"),

    # ---------- Instructor: Feedback tab + detail + exports ----------
    path("app/instructor/booking/<uuid:booking_id>/feedback/", instv.instructor_feedback_tab, name="instructor_feedback_tab"),
    path("app/instructor/booking/<uuid:booking_id>/feedback/poll/", instv.instructor_feedback_poll, name="instructor_feedback_poll"),
    path("app/instructor/feedback/<uuid:pk>/", instv.instructor_feedback_view, name="instructor_feedback_view"),
    path("app/instructor/booking/<uuid:booking_id>/feedback/pdf/all/", instv.instructor_feedback_pdf_all, name="instructor_feedback_pdf_all"),
    path("app/instructor/booking/<uuid:booking_id>/feedback/pdf/summary/", instv.instructor_feedback_pdf_summary, name="instructor_feedback_pdf_summary"),


    path("diag/email/", instv.email_diagnostics, name="email_diagnostics"),

    path("exam/", views_public.delegate_exam_start, name="delegate_exam_start"),
    path("exam/instructors/", views_public.exam_instructors_api, name="exam_instructors_api"),
    path("exam/rules/", views_public.delegate_exam_rules, name="delegate_exam_rules"),
    path("exam/question/<int:pk>/image/", views_public.exam_question_image, name="exam_question_image"),
    path("exam/run/", views_public.delegate_exam_run, name="delegate_exam_run"),
    path("exam/review/", views_public.delegate_exam_review, name="delegate_exam_review"),
    path("exam/finish/", views_public.delegate_exam_finish, name="delegate_exam_finish"),
    path("privacy/", views_public.privacy_notices, name="privacy_notices"),
    path("accident-report/", views.accident_report_public, name="accident_report_public"),
    path("accident-report/day-options/", views.accident_report_day_options_api, name="accident_report_day_options_api"),
    path("accident-report/instructors/", views.accident_report_instructors_api, name="accident_report_instructors_api"),
    path("accident-report/thanks/", views.accident_report_thanks, name="accident_report_thanks"),

    path("accident-reports/<uuid:pk>/", views.accident_report_detail, name="accident_report_detail"),
    path("accident-reports/export-pptx/", views.accident_report_export_pptx, name="accident_report_export_pptx"),
    path("accident-reports/delete/", views.accident_report_delete, name="accident_report_delete"),
    path("accident-reports/poll/", views.accident_report_poll, name="accident_report_poll"),
    path("accident-reports/anonymise", views.accident_report_anonymise, name="accident_report_anonymise"),
    
    path(
        "app/instructor/exams/attempt/<int:attempt_id>/review/",
        views_instructor.instructor_attempt_review,
        name="instructor_attempt_review",
    ),
    path(
        "app/instructor/exams/attempt/<int:attempt_id>/incorrect/",
        views_instructor.instructor_attempt_incorrect,
        name="instructor_attempt_incorrect",
    ),
    path(
        "app/instructor/exams/attempt/<int:attempt_id>/pdf/",
        views_instructor.instructor_attempt_response_pdf,
        name="instructor_attempt_response_pdf",
    ),
    path(
        "app/instructor/booking/<uuid:booking_id>/exams/summary.pdf",
        views_instructor.instructor_exams_summary_pdf,
        name="instructor_exams_summary_pdf",
    ),
    path(
    "app/instructor/exams/attempt/<int:attempt_id>/authorize-retake/",
    instv.instructor_attempt_authorize_retake,
    name="instructor_attempt_authorize_retake",
),
    

    path(
    "app/instructor/whoami/",
    instv.whoami,
    name="instructor_whoami",
),

    path("bookings/<uuid:booking_id>/certificates/preview/", views_certificates.booking_certificates_preview, name="booking_certificates_preview")  ,


        # Engineer
    path("app/engineer/", engv.engineer_dashboard, name="engineer_dashboard"),

    # Inspector
    path("app/inspector/", inspv.inspector_dashboard, name="inspector_dashboard"),

    path("app/no-roles/", views.no_roles_assigned, name="no_roles"),

    path(
        "api/instructor/<uuid:pk>/postcode/",
        views_admin.api_instructor_postcode,
        name="api_instructor_postcode",
    ),

    path("app/admin/site-settings/", site_settings, name="admin_site_settings"),
    path("app/admin/site-settings/meta/<int:pk>/", meta_settings_edit, name="admin_meta_settings_edit"),
    path("app/admin/site-settings/logo-overrides/<int:pk>/", logo_override_edit, name="admin_logo_override_edit"),
    path("app/admin/site-settings/logo-overrides/<int:pk>/delete/", logo_override_delete, name="admin_logo_override_delete"),
    path("app/admin/meta-settings/", site_settings, name="admin_meta_settings"),
    path("app/admin/meta-settings/<int:pk>/", meta_settings_edit, name="admin_meta_settings_edit"),

]



