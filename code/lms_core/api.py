from ninja import NinjaAPI, UploadedFile, File, Form
from ninja.errors import ValidationError
from ninja.pagination import paginate, PageNumberPagination
from ninja.responses import Response
from ninja_simple_jwt.auth.views.api import mobile_auth_router
from ninja_simple_jwt.auth.ninja_auth import HttpJwtAuth
from ninja_extra import NinjaExtraAPI, throttle
from ninja_extra.throttling import UserRateThrottle
from ninja_extra.exceptions import Throttled
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.core.cache import cache
from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.http import condition
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from lms_core.models import Course, CourseMember, CourseContent, Comment
from lms_core.schema import CourseSchemaOut, CourseMemberOut, CourseSchemaIn
from lms_core.schema import CourseContentMini, CourseContentFull
from lms_core.schema import CourseCommentOut, CourseCommentIn
from lms_core.schema import UserRegisterIn, UserOut, EnrollStudentBatch
from lms_core.schema import CommentModerationIn, UserActivityOut,CourseAnalyticsOut
from typing import List
from http import HTTPStatus

# THROTLING
class EnrollBatchThrottle(UserRateThrottle):
    rate = settings.RATE_LIMITS['ENROLL_BATCH']
    scope = "enroll_batch"

class RegisterThrottle(UserRateThrottle):
    rate = settings.RATE_LIMITS['REGISTER']
    scope = "register"

class ModerationThrottle(UserRateThrottle):
    rate = settings.RATE_LIMITS['MODERATION']
    scope = "moderation"

# FILTERING
def filter_scheduled_content(queryset):
    now = timezone.now()
    return queryset.filter(
        Q(scheduled_release__lte=now) | 
        Q(scheduled_release__isnull=True)
    )

apiv1 = NinjaAPI()
apiv1.add_router("/auth/", mobile_auth_router)
apiAuth = HttpJwtAuth()

# ENDPOINT
# AUTH
@apiv1.post("/auth/register", response={201: UserOut, 400: dict, 422: dict}, tags=["Auth"])
@throttle(RegisterThrottle)
def register(request, payload: UserRegisterIn):
    if User.objects.filter(email=payload.email).exists():
        return 400, {"message": "Email sudah terdaftar"}
    try:
        user = User.objects.create_user(
            username=payload.email,
            email=payload.email,
            password=payload.password,
            first_name=payload.first_name,
            last_name=payload.last_name,
            tempat_lahir=payload.tempat_lahir,
            tanggal_lahir=payload.tanggal_lahir,
            alamat=payload.alamat,
            no_hp=payload.no_hp
        )
        return 201, user
    except ValidationError as e:
        return 422, e.errors()

# ENROLL
@apiv1.post("/enroll/batch", auth=apiAuth, response={201: dict, 400: dict, 403: dict}, tags=['Enrollment'])
@throttle(EnrollBatchThrottle)
def enroll_student_batch(request, payload: EnrollStudentBatch):
    course = get_object_or_404(Course, id=payload.course_id)
    teacher = request.auth
    
    # Validasi kepemilikan kursus
    if course.teacher != teacher:
        return Response(
            {"detail": "Akses ditolak. Hanya pemilik kursus yang dapat menambahkan siswa"},
            status=HTTPStatus.FORBIDDEN
        )
    
    # Hitung jumlah siswa saat ini
    current_students = CourseMember.objects.filter(
        course_id=course, 
        roles='std'
    ).count()
    
    success = []
    failed = []
    
    for email in payload.emails:
        # Cek kuota
        if course.max_students and current_students >= course.max_students:
            failed.append({"email": email, "reason": "Kuota kursus penuh"})
            continue
            
        try:
            user = User.objects.get(email=email)
            
            # Cek duplikasi
            if CourseMember.objects.filter(course_id=course, user_id=user).exists():
                failed.append({"email": email, "reason": "Siswa sudah terdaftar"})
                continue
                
            # Enroll siswa
            CourseMember.objects.create(
                course_id=course,
                user_id=user,
                roles='std'
            )
            success.append(email)
            current_students += 1  # Update counter
            
        except User.DoesNotExist:
            failed.append({"email": email, "reason": "Email tidak terdaftar"})
    
    return {
        "course": course.name,
        "success_count": len(success),
        "failed_count": len(failed),
        "success_emails": success,
        "failed_emails": failed
    }
    
# COMMENTS
@apiv1.post("/comments", auth=apiAuth, response={201: CourseCommentOut, 403: dict}, tags=['Comments'])
def create_comment(request, payload: CourseCommentIn, content_id: int):
    content = get_object_or_404(CourseContent, id=content_id)
    if content.scheduled_release and content.scheduled_release > timezone.now():
        return Response(
            {"detail": "Tidak dapat berinteraksi dengan konten yang belum dirilis"},
            status=HTTPStatus.FORBIDDEN
        )
        
    member = get_object_or_404(CourseMember, course_id=content.course_id, user_id=request.auth)
    comment = Comment.objects.create(
        content_id=content,
        member_id=member,
        text=payload.text,
        approved=False  # default belum diapprove
    )
    return 201, CourseCommentOut.from_orm(comment)

@apiv1.patch("/comments/{comment_id}/moderate", auth=apiAuth, response={200: CourseCommentOut, 403: dict, 404: dict}, tags=['Moderation'])
@throttle(ModerationThrottle)
def moderate_comment(request, comment_id: int, payload: CommentModerationIn):
    comment = get_object_or_404(Comment, id=comment_id)
    course = comment.content_id.course_id
    
    if course.teacher != request.auth:
        return Response(
            {"detail": "Akses ditolak. Hanya pemilik kursus yang dapat memoderasi komentar"},
            status=HTTPStatus.FORBIDDEN
        )
    
    comment.approved = payload.approved
    comment.save()
    
    return CourseCommentOut.from_orm(comment)

@apiv1.get("/contents/{content_id}/comments", response=List[CourseCommentOut], auth=apiAuth, tags=['Comments'])
def get_comments(request, content_id: int, approved: bool = None, page: int = 1):
    comments = Comment.objects.filter(content_id=content_id)
    
    content = get_object_or_404(CourseContent, id=content_id)
    if content.scheduled_release and content.scheduled_release > timezone.now():
        return Response(
            {"detail": "Konten belum dirilis"},
            status=HTTPStatus.FORBIDDEN
        )
        
    if approved is not None:
        comments = comments.filter(approved=approved)
    
    paginator = PageNumberPagination()
    paginator.page_size = 10
    result_page = paginator.paginate_queryset(comments, request)
    
    return [CourseCommentOut.from_orm(c) for c in result_page]

# DASHBOARD
@apiv1.get("/dashboard", response=UserActivityOut, auth=apiAuth, tags=['Dashboard'])
def get_user_activity(request, user_id: int):
    user = get_object_or_404(User, id=user_id)
    
    if not request.auth.id and request.auth.id != user_id:
        return Response(
            {"detail": "Akses ditolak. Hanya pemilik akun dan admin yang bisa melihat data statistik user"},
            status=HTTPStatus.FORBIDDEN
        )
    
    return {
        "student_enrollment_count": CourseMember.objects.filter(
            user_id=user, 
            roles='std'
        ).count(),
        "teacher_course_count": Course.objects.filter(teacher=user).count(),
        "comment_count": Comment.objects.filter(member_id__user_id=user).count(),
        "completed_content_count": 0  # Fitur belum diimplementasi
    }
    
# COURSES
@apiv1.get("/courses/{course_id}/contents", response=List[CourseContentFull], auth=apiAuth, tags=['Course Content'])
def get_course_contents(request, course_id: int):
    course = get_object_or_404(Course, id=course_id)
    contents = CourseContent.objects.filter(course_id=course)
    contents = filter_scheduled_content(contents)
    return contents

@apiv1.get("/courses/{course_id}/analytics", 
           response={200: CourseAnalyticsOut, 403: dict, 404: dict}, 
           auth=apiAuth, 
           tags=['Analytics'])
def get_course_analytics(request, course_id: int):
    course = get_object_or_404(Course, id=course_id)
    user = request.auth
    
    if course.teacher != user and not user.is_superuser:
        return Response(
            {"detail": "Akses ditolak. Hanya pemilik kursus atau admin yang dapat melihat statistik"},
            status=HTTPStatus.FORBIDDEN
        )
    
    return {
        "course_id": course.id,
        "member_count": CourseMember.objects.filter(course_id=course).count(),
        "content_count": CourseContent.objects.filter(course_id=course).count(),
        "comment_count": Comment.objects.filter(content_id__course_id=course).count()
    }

# EXCEPTION HANDLING
@apiv1.exception_handler(Throttled)
def throttled_exception_handler(request, exc):
    return Response(
        {
            "detail": "Terlalu banyak request. Silakan coba lagi nanti.",
            "wait_seconds": exc.wait
        },
        status=429
    )
