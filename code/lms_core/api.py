from ninja import NinjaAPI, UploadedFile, File, Form
from ninja.errors import ValidationError, HttpError
from ninja.pagination import paginate, PageNumberPagination
from ninja.responses import Response
from ninja_simple_jwt.auth.views.api import mobile_auth_router, web_auth_router
from ninja_simple_jwt.auth.ninja_auth import HttpJwtAuth
from ninja_extra import NinjaExtraAPI, throttle
from ninja_extra.throttling import UserRateThrottle
from ninja_extra.exceptions import Throttled
from django.contrib.auth import get_user_model
# from django.contrib.auth.models import User
from django.core.cache import cache
from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.http import condition
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.template.loader import render_to_string
from django.http import HttpResponse
from lms_core.models import Course, CourseMember, CourseContent, Comment
from lms_core.models import CourseCompletion, ContentCompletion, User
from lms_core.schema import CourseSchemaOut, CourseMemberOut, CourseSchemaIn
from lms_core.schema import CourseContentMini, CourseContentFull
from lms_core.schema import CourseCommentOut, CourseCommentIn
from lms_core.schema import UserRegisterIn, UserOut, EnrollStudentBatch
from lms_core.schema import CommentModerationIn, UserActivityOut,CourseAnalyticsOut
from lms_core.schema import UserProfileOut, UserProfileUpdate, CourseContentIn
from typing import List
from http import HTTPStatus
from xhtml2pdf import pisa
from io import BytesIO

# from django.contrib.auth import get_user_model
# User = get_user_model()

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

class CertificateThrottle(UserRateThrottle):
    rate = settings.RATE_LIMITS['CERTIFICATE']
    scope = "certificate"

class CourseCreationThrottle(UserRateThrottle):
    rate = settings.RATE_LIMITS['COURSE_CREATION']
    scope = "course_creation"

# FILTERING
def filter_scheduled_content(queryset):
    now = timezone.now()
    return queryset.filter(
        (Q(scheduled_release__lte=now) | Q(scheduled_release__isnull=True)) &
        (Q(scheduled_close__gte=now) | Q(scheduled_close__isnull=True))
    )

apiv1 = NinjaAPI()
apiv1.add_router("/auth/", mobile_auth_router)
apiv1.add_router("/auth/web/", web_auth_router)
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
@apiv1.post("/enroll/batch", auth=apiAuth, response={200:dict, 201: dict, 400: dict, 403: dict}, tags=['Enrollment'])
@throttle(EnrollBatchThrottle)
def enroll_student_batch(request, payload: EnrollStudentBatch):
    course = get_object_or_404(Course, id=payload.course_id)
    teacher = request.auth
    
    # Validasi kepemilikan kursus
    if course.teacher_id != teacher.id:
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
@apiv1.post("/comments/{content_id}", auth=apiAuth, response={201: CourseCommentOut, 403: dict}, tags=['Comments'])
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
        comment=payload.comment,
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
@paginate(PageNumberPagination, page_size=10)
def get_comments(request, content_id: int, approved: bool = None):
    comments = Comment.objects.filter(content_id=content_id)
    
    content = get_object_or_404(CourseContent, id=content_id)
    if content.scheduled_release and content.scheduled_release > timezone.now():
        return Response(
            {"detail": "Konten belum dirilis"},
            status=HTTPStatus.FORBIDDEN
        )
        
    if approved is not None:
        comments = comments.filter(approved=approved)
    
    return comments

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
        "completed_content_count": ContentCompletion.objects.filter(user=user).count()  # Diperbarui
    }
    
# COURSES
@apiv1.post("/courses", 
            auth=apiAuth, 
            response={201: CourseSchemaOut, 400: dict, 422: dict}, 
            tags=['Courses'])
@throttle(CourseCreationThrottle)
def create_course(request, payload: CourseSchemaIn):
    try:
        # Pastikan request.auth adalah instance User
        if not isinstance(request.auth, User):
            return Response(
                {"detail": "Invalid user authentication"},
                status=HTTPStatus.UNAUTHORIZED
            )
        
        # Buat course baru dengan teacher = user yang sedang login
        course = Course.objects.create(
            teacher=request.auth, 
            name=payload.name,
            description=payload.description,
            price=payload.price,
            max_students=payload.max_students
        )
        return Response(CourseSchemaOut.from_orm(course), status=HTTPStatus.CREATED)
    except ValidationError as e:
        return Response(e.errors(), status=HTTPStatus.UNPROCESSABLE_ENTITY)
    except Exception as e:
        return Response({"detail": str(e)}, status=HTTPStatus.BAD_REQUEST)
    
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

@apiv1.post("/courses/{course_id}/complete", auth=apiAuth, response={201: dict, 403: dict, 404: dict}, tags=['Completion'])
def mark_course_complete(request, course_id: int):
    course = get_object_or_404(Course, id=course_id)
    user = request.auth
    
    # Cek apakah user adalah member course
    if not CourseMember.objects.filter(course_id=course, user_id=user).exists():
        return Response(
            {"detail": "Anda tidak terdaftar di kursus ini"},
            status=HTTPStatus.FORBIDDEN
        )
    
    # Cek apakah sudah pernah selesai
    if CourseCompletion.objects.filter(user=user, course=course).exists():
        return Response(
            {"detail": "Anda sudah menyelesaikan kursus ini sebelumnya"},
            status=HTTPStatus.BAD_REQUEST
        )
    
    # Buat penyelesaian kursus
    CourseCompletion.objects.create(user=user, course=course)
    return Response(
        {"detail": "Kursus berhasil ditandai sebagai selesai"},
        status=HTTPStatus.CREATED
    )

# SERTIFIKAT HTML
@apiv1.get("/certificates/{course_id}", auth=apiAuth, tags=['Certificate'])
@throttle(CertificateThrottle)
def view_certificate(request, course_id: int):
    course = get_object_or_404(Course, id=course_id)
    user = request.auth
    
    completion = get_object_or_404(CourseCompletion, user=user, course=course)
    
    context = {
        'NAMA_USER': f"{user.first_name} {user.last_name}",
        'NAMA_COURSE': course.name,
        'tanggal_penyelesaian': completion.completed_at.strftime("%d %B %Y")
    }
    
    # Render template HTML
    html_content = render_to_string('certification.html', context)
    return HttpResponse(html_content)

# # ENDPOINT UNDUH SERTIFIKAT PDF
# @apiv1.get("/certificates/{course_id}/pdf", auth=apiAuth, tags=['Certificate'])
# @throttle(CertificateThrottle)
# def download_certificate_pdf(request, course_id: int):
#     course = get_object_or_404(Course, id=course_id)
#     user = request.auth
    
#     completion = get_object_or_404(CourseCompletion, user=user, course=course)
    
#     context = {
#         'NAMA_USER': f"{user.first_name} {user.last_name}",
#         'NAMA_COURSE': course.name,
#         'tanggal_penyelesaian': completion.completed_at.strftime("%d %B %Y")
#     }
    
#     # Render template HTML
#     html_content = render_to_string('certificate_template.html', context)
    
#     # Konversi HTML ke PDF
#     response = HttpResponse(content_type='application/pdf')
#     response['Content-Disposition'] = f'attachment; filename="sertifikat_{course.name}.pdf"'
    
#     # Buat PDF
#     pdf_status = pisa.CreatePDF(html_content, dest=response)
    
#     if pdf_status.err:
#         return HttpResponse('Error generating PDF', status=500)
#     return response

# PROFILE
@apiv1.get("/profile", response={200: UserProfileOut, 404: dict}, auth=apiAuth, tags=['Profile'])
def show_profile(request):
    user = get_object_or_404(User, id=request.auth.id)
    
    # Dapatkan kursus yang diikuti user
    enrolled_courses = Course.objects.filter(
        coursemember__user_id=user, 
        coursemember__roles='std'
    )
    
    # Dapatkan kursus yang diajarkan user
    taught_courses = Course.objects.filter(teacher=user)
    
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "no_hp": user.no_hp,
        "deskripsi": user.deskripsi if hasattr(user, 'deskripsi') else None,
        "foto_profil": user.foto_profil.url if user.foto_profil else None,
        "enrolled_courses": enrolled_courses,
        "taught_courses": taught_courses
    }

@apiv1.patch("/profile/edit", response={200: UserProfileOut, 400: dict}, auth=apiAuth, tags=['Profile'])
def edit_profile(request, payload: UserProfileUpdate):
    user = request.auth
    
    # Update hanya field yang disediakan
    update_data = payload.dict(exclude_unset=True)
    
    # Validasi email unik
    if 'email' in update_data and update_data['email'] != user.email:
        if User.objects.filter(email=update_data['email']).exists():
            return Response(
                {"detail": "Email sudah digunakan oleh pengguna lain"},
                status=HTTPStatus.BAD_REQUEST
            )
        user.username = update_data['email']  # Update username juga
    
    # Update field
    for field, value in update_data.items():
        setattr(user, field, value)
    
    user.save()
    # Kembalikan profil terbaru
    return show_profile(request)

# CONTENT
@apiv1.post("/courses/{course_id}/contents", auth=apiAuth,
           response={201: CourseContentFull, 400: dict, 403: dict},
           tags=['Course Content'])
def create_course_content(request, course_id: int, payload: CourseContentIn):
    # Validasi kepemilikan course
    course = get_object_or_404(Course, id=course_id)
    if course.teacher != request.auth:
        return Response(
            {"detail": "Akses ditolak. Hanya pemilik kursus yang dapat menambahkan konten"},
            status=HTTPStatus.FORBIDDEN
        )
    
    # Create content
    content = CourseContent.objects.create(
        course_id=course,
        name=payload.name,
        description=payload.description,
        video_url=payload.video_url,
        file_attachment=payload.file_attachment,
        scheduled_release=payload.scheduled_release,
        scheduled_close=payload.scheduled_close
    )
    
    return Response(CourseContentFull.from_orm(content), status=HTTPStatus.CREATED)
        
@apiv1.post("/contents/{content_id}/complete", auth=apiAuth, response={201: dict, 400: dict, 403: dict, 404: dict}, tags=['Completion'])
def mark_content_complete(request, content_id: int):
    content = get_object_or_404(CourseContent, id=content_id)
    user = request.auth
    
    # Cek apakah user adalah member course
    if not CourseMember.objects.filter(course_id=content.course_id, user_id=user, roles='std').exists():
        return Response(
            {"detail": "Anda tidak terdaftar di kursus ini"},
            status=HTTPStatus.FORBIDDEN
        )
    
    # Cek apakah sudah pernah selesai
    if ContentCompletion.objects.filter(user=user, content=content).exists():
        return Response(
            {"detail": "Anda sudah menyelesaikan konten ini sebelumnya"},
            status=HTTPStatus.BAD_REQUEST
        )
    
    # Buat penyelesaian konten
    ContentCompletion.objects.create(user=user, content=content)
    return Response(
        {"detail": "Konten berhasil ditandai sebagai selesai"},
        status=HTTPStatus.CREATED
    )

@apiv1.get("/courses/{course_id}/completions", response=List[CourseContentFull], auth=apiAuth, tags=['Completion'])
@paginate(PageNumberPagination)
def get_content_completions(request, course_id: int, **kwargs):
    course = get_object_or_404(Course, id=course_id)
    user = request.auth

    # Cek apakah user adalah member course
    if not CourseMember.objects.filter(course_id=course, user_id=user, roles='std').exists():
        raise HttpError(403, "Anda tidak terdaftar di kursus ini")

    # Dapatkan konten yang sudah diselesaikan oleh user di course ini
    completed_contents = CourseContent.objects.filter(
        course_id=course,
        contentcompletion__user=user
    )
    return completed_contents

@apiv1.delete("/contents/{content_id}/complete", auth=apiAuth, response={204: dict, 403: dict, 404: dict}, tags=['Completion'])
def unmark_content_complete(request, content_id: int):
    content = get_object_or_404(CourseContent, id=content_id)
    user = request.auth
    
    # Cek apakah user adalah member course
    if not CourseMember.objects.filter(course_id=content.course_id, user_id=user, roles='std').exists():
        return Response(
            {"detail": "Anda tidak terdaftar di kursus ini"},
            status=HTTPStatus.FORBIDDEN
        )
    
    completion = get_object_or_404(ContentCompletion, user=user, content=content)
    completion.delete()
    return Response({},status=HTTPStatus.NO_CONTENT)

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
