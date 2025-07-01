from django.test import TestCase
from django.urls import reverse
# from django.contrib.auth.models import User
from lms_core.utils import calculate_discount, validate_password, calculator
from ninja_extra.testing import TestAsyncClient
from ninja.testing import TestClient
from lms_core.models import Course, CourseMember, CourseContent, Comment
from lms_core.models import CourseCompletion, ContentCompletion, User
from lms_core.schema import EnrollStudentBatch
from lms_core.api import apiv1
import json

class RegisterTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.payload = {
            "username": "testtest",
            "email": "test@example.com",
            "password": "securepassword123",
            "first_name": "Test",
            "last_name": "User",
            "tempat_lahir": "Jakarta",
            "tanggal_lahir": "1990-01-01",
            "alamat": "Jl. Sudirman No. 1",
            "no_hp": "081234567890"
        }
    
    def test_register(self):        # Test successful registration
        response = self.client.post("/api/auth/register", json=self.payload)
        self.assertEqual(response.status_code, 201)
        
        self.assertIn("email", response.json())
        self.assertEqual(response.json()["email"], self.payload["email"])

    def test_negative_register(self):
        self.client.post("/api/auth/register", json=self.payload)
        response = self.client.post("/api/auth/register", json=self.payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("message", response.json())
        self.assertIn("terdaftar", response.json()["message"].lower())

        bad_payload = self.payload.copy()
        bad_payload.pop("password")
        response = self.client.post("/api/auth/register", json=bad_payload)
        self.assertIn(response.status_code, [400, 422])
        
        self.assertTrue("message" in response.json() or "errors" in response.json() or "detail" in response.json())
    

    def test_rate_limiting(self):
        responses = []
        for _ in range(6):
            response = self.client.post("/api/auth/register", json=self.payload)
            responses.append(response.status_code)
            self.payload["email"] = f"test{_}@example.com"

        
        self.assertEqual(responses[:5], [201 or 400 or 422] * 5)
        self.assertEqual(responses[5], 429)
        self.assertIn("Terlalu banyak", responses[5].json()["detail"])
        

class EnrollBatchTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.teacher = User.objects.create_user(
            username='guru', email='guru@sekolah.id', password='password'
        )
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        # Buat token akses
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "guru", "password": "password"}
        )
        self.token = token_res.json()["access"]
    
    def test_success_enroll(self):
        User.objects.create_user(username='s1', email='s1@sekolah.id', password='pass')
        User.objects.create_user(username='s2', email='s2@sekolah.id', password='pass')
        
        payload = {
            "course_id": self.course.id,
            "emails": ["s1@sekolah.id", "s2@sekolah.id"]
        }
        
        response = self.client.post(
            "/enroll/batch",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['success_count'], 2)
        self.assertEqual(CourseMember.objects.count(), 2)
    
    def test_failed_enroll(self):
        payload = {
            "course_id": self.course.id,
            "emails": ["invalid_email", "unknown@mail.com"]
        }
        
        response = self.client.post(
            "/enroll/batch",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 201)  # Tetap 201 karena partial success
        data = response.json()
        self.assertEqual(data['failed_count'], 2)
        self.assertIn("Email tidak valid", data['failed_emails'][0]['reason'])
    
    def test_rate_limiting(self):
        payload = {
            "course_id": self.course.id,
            "emails": ["test@mail.com"]
        }
        
        responses = []
        for _ in range(11):  # 10 request allowed + 1 extra
            res = self.client.post(
                "/enroll/batch",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"}
            )
            responses.append(res.status_code)
        
        self.assertEqual(responses[-1], 429)
        self.assertIn("Terlalu banyak", responses[-1].json()["detail"])
    # ... tambahkan di class EnrollBatchTest ...

    def test_enrollment_with_quota(self):
        # Set kuota kursus
        self.course.max_students = 1
        self.course.save()
        
        # Buat satu siswa pertama
        student1 = User.objects.create_user(
            username='s1', email='s1@sekolah.id', password='pass'
        )
        CourseMember.objects.create(
            course_id=self.course,
            user_id=student1,
            roles='std'
        )
        
        # Coba enroll dua siswa baru
        User.objects.create_user(username='s2', email='s2@sekolah.id', password='pass')
        User.objects.create_user(username='s3', email='s3@sekolah.id', password='pass')
        
        payload = {
            "course_id": self.course.id,
            "emails": ["s2@sekolah.id", "s3@sekolah.id"]
        }
        
        response = self.client.post(
            "/enroll/batch",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['success_count'], 0)
        self.assertEqual(data['failed_count'], 2)
        self.assertEqual(data['failed_emails'][0]['reason'], "Kuota kursus penuh")
        
    def test_enrollment_partial_quota(self):
        # Set kuota kursus
        self.course.max_students = 2
        self.course.save()
        
        # Buat satu siswa pertama
        student1 = User.objects.create_user(
            username='s1', email='s1@sekolah.id', password='pass'
        )
        CourseMember.objects.create(
            course_id=self.course,
            user_id=student1,
            roles='std'
        )
        
        # Enroll dua siswa baru (satu harus berhasil, satu gagal karena kuota)
        User.objects.create_user(username='s2', email='s2@sekolah.id', password='pass')
        User.objects.create_user(username='s3', email='s3@sekolah.id', password='pass')
        
        payload = {
            "course_id": self.course.id,
            "emails": ["s2@sekolah.id", "s3@sekolah.id"]
        }
        
        response = self.client.post(
            "/enroll/batch",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['success_count'], 1)
        self.assertEqual(data['failed_count'], 1)
        self.assertEqual(data['success_emails'], ["s2@sekolah.id"])
        self.assertEqual(data['failed_emails'][0]['reason'], "Kuota kursus penuh")
        
    def test_enrollment_without_quota(self):
        # Pastikan tidak ada kuota (null)
        self.course.max_students = None
        self.course.save()
        
        # Enroll 3 siswa
        emails = []
        for i in range(3):
            email = f"student{i}@sekolah.id"
            User.objects.create_user(username=f"s{i}", email=email, password='pass')
            emails.append(email)
        
        payload = {
            "course_id": self.course.id,
            "emails": emails
        }
        
        response = self.client.post(
            "/enroll/batch",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data['success_count'], 3)
        self.assertEqual(CourseMember.objects.count(), 3)


class CommentModerationTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.teacher = User.objects.create_user(
            username='guru', email='guru@sekolah.id', password='password'
        )
        self.student = User.objects.create_user(
            username='siswa', email='siswa@sekolah.id', password='password'
        )
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        self.content = CourseContent.objects.create(
            name="Konten 1", description="Deskripsi", course_id=self.course
        )
        self.course_member = CourseMember.objects.create(
            course_id=self.course, user_id=self.student, roles='std'
        )
        self.comment = Comment.objects.create(
            content_id=self.content, 
            member_id=self.course_member, 
            comment="Komentar pertama"
        )
        
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "guru", "password": "password"}
        )
        self.token = token_res.json()["access"]
    
    def test_success_moderation(self):
        payload = {"is_approved": True}
        response = self.client.patch(
            f"/comments/{self.comment.id}/moderate",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["approved"])
        
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.approved)
    
    def test_fail_moderation_by_non_owner(self):
        other_teacher = User.objects.create_user(
            username='guru2', email='guru2@sekolah.id', password='password'
        )
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "guru2", "password": "password"}
        )
        other_token = token_res.json()["access"]
        
        payload = {"approved": True}
        response = self.client.patch(
            f"/comments/{self.comment.id}/moderate",
            json=payload,
            headers={"Authorization": f"Bearer {other_token}"}
        )
        
        self.assertEqual(response.status_code, 403)
        self.assertIn("Akses ditolak", response.json()["detail"])
    
    def test_filtering_and_pagination(self):
        for i in range(15):
            Comment.objects.create(
                content_id=self.content,
                member_id=self.course_member,
                comment=f"Komentar {i}",
                approved=(i % 2 == 0)  # Setengah approved
            )
        
        response = self.client.get(
            f"/contents/{self.content.id}/comments?approved=true&page=1",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 8)  # 8 approved di page 1
        self.assertTrue(all(c['approved'] for c in data))
        
        response = self.client.get(
            f"/contents/{self.content.id}/comments?page=2",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(len(response.json()), 5)  # Sisa 5 komentar
    
    def test_throttling(self):
        payload = {"approved": True}
        responses = []
        
        for i in range(32):  # 30 allowed + 2 extra
            response = self.client.patch(
                f"/comments/{self.comment.id}/moderate",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"}
            )
            responses.append(response.status_code)
        
        # Request ke-31 harus kena throttle
        self.assertEqual(responses[30], 429)
        self.assertIn("Terlalu banyak", responses[30].json()["detail"])

class UserActivityDashboardTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        # Buat admin
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@sekolah.id', password='password'
        )
        # Buat user biasa
        self.user = User.objects.create_user(
            username='user', email='user@sekolah.id', password='password'
        )
        # Buat teacher
        self.teacher = User.objects.create_user(
            username='teacher', email='teacher@sekolah.id', password='password'
        )
        
        # Buat course oleh teacher
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        
        # Enroll user sebagai student
        CourseMember.objects.create(
            course_id=self.course, user_id=self.user, roles='std'
        )
        
        # Buat konten
        self.content = CourseContent.objects.create(
            name="Konten 1", description="Deskripsi", course_id=self.course
        )
        
        # Buat komentar
        member = CourseMember.objects.get(course_id=self.course, user_id=self.user)
        Comment.objects.create(
            content_id=self.content, 
            member_id=member, 
            comment="Komentar pertama"
        )
        
        # Token untuk admin
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "admin", "password": "password"}
        )
        self.admin_token = token_res.json()["access"]
        
        # Token untuk user
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "user", "password": "password"}
        )
        self.user_token = token_res.json()["access"]
    
    def test_success_admin_view(self):
        response = self.client.get(
            f"/dashboard?user_id={self.user.id}",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['student_enrollment_count'], 1)
        self.assertEqual(data['teacher_course_count'], 0)
        self.assertEqual(data['comment_count'], 1)
    
    def test_success_user_view_own_data(self):
        response = self.client.get(
            f"/dashboard?user_id={self.user.id}",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['student_enrollment_count'], 1)
    
    def test_fail_unauthorized_access(self):
        # User biasa mencoba akses data admin
        response = self.client.get(
            f"/dashboard?user_id={self.admin.id}",
            headers={"Authorization": f"Bearer {self.user_token}"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Akses ditolak", response.json()["detail"])
    
    def test_filtering_by_user_id(self):
        # Buat user kedua
        user2 = User.objects.create_user(
            username='user2', email='user2@sekolah.id', password='password'
        )
        
        # Admin filter berdasarkan user_id
        response = self.client.get(
            f"/dashboard?user_id={user2.id}",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['student_enrollment_count'], 0)
    
    def test_user_not_found(self):
        response = self.client.get(
            "/dashboard?user_id=999",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    def test_success_admin_view(self):
        # Buat beberapa penyelesaian konten
        ContentCompletion.objects.create(user=self.user, content=self.content)
        ContentCompletion.objects.create(user=self.user, content=self.content)
        
        content2 = CourseContent.objects.create(
            name="Konten 2", description="Deskripsi", course_id=self.course
        )
        ContentCompletion.objects.create(user=self.user, content=content2)
        
        response = self.client.get(
            f"/dashboard?user_id={self.user.id}",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['completed_content_count'], 2)

class CourseAnalyticsTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        
        # Buat admin
        self.admin = User.objects.create_superuser(
            username='admin', email='admin@sekolah.id', password='password'
        )
        
        # Buat teacher
        self.teacher = User.objects.create_user(
            username='teacher', email='teacher@sekolah.id', password='password'
        )
        
        # Buat course oleh teacher
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        
        # Tambahkan beberapa konten
        for i in range(3):
            CourseContent.objects.create(
                name=f"Konten {i}", 
                description="Deskripsi", 
                course_id=self.course
            )
        
        # Tambahkan beberapa member
        for i in range(5):
            student = User.objects.create_user(
                username=f'student{i}', 
                email=f'student{i}@sekolah.id', 
                password='password'
            )
            CourseMember.objects.create(
                course_id=self.course, 
                user_id=student, 
                roles='std'
            )
        
        # Tambahkan komentar
        content = CourseContent.objects.first()
        member = CourseMember.objects.first()
        for i in range(10):
            Comment.objects.create(
                content_id=content, 
                member_id=member, 
                comment=f"Komentar {i}"
            )
        
        # Token untuk admin
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "admin", "password": "password"}
        )
        self.admin_token = token_res.json()["access"]
        
        # Token untuk teacher
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "teacher", "password": "password"}
        )
        self.teacher_token = token_res.json()["access"]
    
    def test_success_teacher_view(self):
        response = self.client.get(
            f"/courses/{self.course.id}/analytics",
            headers={"Authorization": f"Bearer {self.teacher_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['member_count'], 5)
        self.assertEqual(data['content_count'], 3)
        self.assertEqual(data['comment_count'], 10)
    
    def test_success_admin_view(self):
        response = self.client.get(
            f"/courses/{self.course.id}/analytics",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['member_count'], 5)
    
    def test_fail_unauthorized_access(self):
        # Buat user biasa
        user = User.objects.create_user(
            username='user', email='user@sekolah.id', password='password'
        )
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "user", "password": "password"}
        )
        user_token = token_res.json()["access"]
        
        response = self.client.get(
            f"/courses/{self.course.id}/analytics",
            headers={"Authorization": f"Bearer {user_token}"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Akses ditolak", response.json()["detail"])
    
    def test_course_not_found(self):
        response = self.client.get(
            "/courses/999/analytics",
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    def test_filtering_by_course_id(self):
        # Buat course kedua
        course2 = Course.objects.create(
            name="Fisika", description="Mekanika", price=0, teacher=self.teacher
        )
        
        response = self.client.get(
            f"/courses/{course2.id}/analytics",
            headers={"Authorization": f"Bearer {self.teacher_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['member_count'], 0)
        self.assertEqual(data['content_count'], 0)

class ContentSchedulingTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.teacher = User.objects.create_user(
            username='guru', email='guru@sekolah.id', password='password'
        )
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        
        # Buat token akses
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "guru", "password": "password"}
        )
        self.token = token_res.json()["access"]
    
    def test_scheduled_content_access(self):
        from django.utils import timezone
        from datetime import timedelta
        
        # Konten yang sudah dirilis
        released_content = CourseContent.objects.create(
            name="Konten Sudah Rilis",
            course_id=self.course,
            scheduled_release=timezone.now() - timedelta(days=1)
        )
        
        # Konten yang belum dirilis
        unreleased_content = CourseContent.objects.create(
            name="Konten Belum Rilis",
            course_id=self.course,
            scheduled_release=timezone.now() + timedelta(days=1)
        )
        
        # Test akses konten yang sudah dirilis
        response = self.client.get(
            f"/courses/{self.course.id}/contents",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        contents = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(c['name'] == "Konten Sudah Rilis" for c in contents))
        
        # Test konten yang belum dirilis tidak muncul
        self.assertFalse(any(c['name'] == "Konten Belum Rilis" for c in contents))
        
        # Test akses langsung ke konten yang belum dirilis
        response = self.client.get(
            f"/contents/{unreleased_content.id}/comments",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("belum dirilis", response.json()["detail"])
    
    def test_closed_content_access(self):
        from django.utils import timezone
        from datetime import timedelta
        
        # Konten yang sudah tutup
        closed_content = CourseContent.objects.create(
            name="Konten Sudah Tutup",
            course_id=self.course,
            scheduled_release=timezone.now() - timedelta(days=2),
            scheduled_close=timezone.now() - timedelta(days=1)
        )
        
        # Konten yang belum tutup
        open_content = CourseContent.objects.create(
            name="Konten Masih Terbuka",
            course_id=self.course,
            scheduled_release=timezone.now() - timedelta(days=1),
            scheduled_close=timezone.now() + timedelta(days=1)
        )
        
        response = self.client.get(
            f"/courses/{self.course.id}/contents",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        contents = response.json()
        
        # Hanya konten yang masih terbuka yang muncul
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]['name'], "Konten Masih Terbuka")
        
        # Coba akses konten yang sudah tutup
        response = self.client.get(
            f"/contents/{closed_content.id}/comments",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 403)
                         
    def test_content_without_schedule(self):
        # Konten tanpa jadwal rilis
        content = CourseContent.objects.create(
            name="Konten Tanpa Jadwal",
            course_id=self.course,
            scheduled_release=None
        )
        
        response = self.client.get(
            f"/courses/{self.course.id}/contents",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        contents = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(c['name'] == "Konten Tanpa Jadwal" for c in contents))

class CertificateTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.teacher = User.objects.create_user(
            username='guru', email='guru@sekolah.id', password='password',
            first_name='John', last_name='Doe'
        )
        self.student = User.objects.create_user(
            username='siswa', email='siswa@sekolah.id', password='password',
            first_name='Jane', last_name='Smith'
        )
        
        # Buat course
        self.course = Course.objects.create(
            name="Matematika Lanjutan", description="Aljabar", price=0, teacher=self.teacher
        )
        
        # Enroll student
        CourseMember.objects.create(
            course_id=self.course, user_id=self.student, roles='std'
        )
        
        # Token untuk student
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "siswa", "password": "password"}
        )
        self.student_token = token_res.json()["access"]
    
    def test_mark_course_complete(self):
        # Tandai kursus selesai
        response = self.client.post(
            f"/courses/{self.course.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(CourseCompletion.objects.filter(user=self.student, course=self.course).exists())
    
    def test_view_certificate_success(self):
        # Tandai kursus selesai dulu
        CourseCompletion.objects.create(user=self.student, course=self.course)
        
        response = self.client.get(
            f"/certificates/{self.course.id}",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SERTIFIKAT KELULUSAN", response.content)
        self.assertIn(b"Jane Smith", response.content)
        self.assertIn(b"Matematika Lanjutan", response.content)
    
    def test_download_certificate_success(self):
        # Tandai kursus selesai dulu
        CourseCompletion.objects.create(user=self.student, course=self.course)
        
        response = self.client.get(
            f"/certificates/{self.course.id}/pdf",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        self.assertIn(b'PDF', response.content)  # Cek signature file PDF
    
    def test_certificate_access_denied(self):
        # Buat user lain
        other_user = User.objects.create_user(
            username='user2', email='user2@sekolah.id', password='password'
        )
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "user2", "password": "password"}
        )
        other_token = token_res.json()["access"]
        
        # Tandai kursus selesai oleh student pertama
        CourseCompletion.objects.create(user=self.student, course=self.course)
        
        # User lain mencoba akses sertifikat
        response = self.client.get(
            f"/certificates/{self.course.id}",
            headers={"Authorization": f"Bearer {other_token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    def test_certificate_not_completed(self):
        # Akses sertifikat tanpa menyelesaikan kursus
        response = self.client.get(
            f"/certificates/{self.course.id}",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    def test_throttling(self):
        # Tandai kursus selesai dulu
        CourseCompletion.objects.create(user=self.student, course=self.course)
        
        responses = []
        for _ in range(6):  # 5 request allowed + 1 extra
            res = self.client.get(
                f"/certificates/{self.course.id}",
                headers={"Authorization": f"Bearer {self.student_token}"}
            )
            responses.append(res.status_code)
        
        # Request ke-6 harus kena throttle
        self.assertEqual(responses[5], 429)
        self.assertIn("Terlalu banyak", responses[5].json()["detail"])

class ProfileTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.user = User.objects.create_user(
            username='user@sekolah.id',
            email='user@sekolah.id',
            password='password',
            first_name='John',
            last_name='Doe',
            no_hp='081234567890',
            deskripsi='Deskripsi awal'
        )
        self.other_user = User.objects.create_user(
            username='other@sekolah.id',
            email='other@sekolah.id',
            password='password'
        )
        
        # Buat kursus untuk testing
        self.course1 = Course.objects.create(
            name="Kursus 1", description="Deskripsi", price=0, teacher=self.user
        )
        self.course2 = Course.objects.create(
            name="Kursus 2", description="Deskripsi", price=0, teacher=self.other_user
        )
        
        # Enroll user ke kursus
        CourseMember.objects.create(
            course_id=self.course2,
            user_id=self.user,
            roles='std'
        )
        
        # Token untuk user
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "user@sekolah.id", "password": "password"}
        )
        self.token = token_res.json()["access"]
    
    # TEST SHOW PROFILE
    def test_show_profile_success(self):
        response = self.client.get(
            f"/profile/{self.user.id}",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['first_name'], 'John')
        self.assertEqual(data['last_name'], 'Doe')
        self.assertEqual(data['no_hp'], '081234567890')
        self.assertEqual(data['deskripsi'], 'Deskripsi awal')
        self.assertEqual(len(data['taught_courses']), 1)
        self.assertEqual(len(data['enrolled_courses']), 1)
    
    def test_show_profile_not_found(self):
        response = self.client.get(
            "/profile/999",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    # TEST EDIT PROFILE
    def test_edit_profile_success(self):
        payload = {
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "newemail@sekolah.id",
            "no_hp": "089876543210",
            "deskripsi": "Deskripsi baru"
        }
        response = self.client.patch(
            "/profile",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['first_name'], 'Jane')
        self.assertEqual(data['last_name'], 'Smith')
        self.assertEqual(data['email'], 'newemail@sekolah.id')
        self.assertEqual(data['no_hp'], '089876543210')
        self.assertEqual(data['deskripsi'], 'Deskripsi baru')
        
        # Verifikasi di database
        user = User.objects.get(id=self.user.id)
        self.assertEqual(user.first_name, 'Jane')
        self.assertEqual(user.email, 'newemail@sekolah.id')
        self.assertEqual(user.username, 'newemail@sekolah.id')
    
    def test_edit_profile_duplicate_email(self):
        payload = {"email": self.other_user.email}
        response = self.client.patch(
            "/profile",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Email sudah digunakan", response.json()["detail"])
    
    def test_edit_profile_partial_update(self):
        payload = {"no_hp": "081122334455"}
        response = self.client.patch(
            "/profile",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['no_hp'], '081122334455')
        # Field lain tetap sama
        self.assertEqual(data['first_name'], 'John')
        self.assertEqual(data['email'], 'user@sekolah.id')

class ContentCompletionTest(TestCase):
    def setUp(self):
        self.client = TestClient(apiv1)
        self.teacher = User.objects.create_user(
            username='guru', email='guru@sekolah.id', password='password'
        )
        self.student = User.objects.create_user(
            username='siswa', email='siswa@sekolah.id', password='password'
        )
        self.course = Course.objects.create(
            name="Matematika", description="Aljabar", price=0, teacher=self.teacher
        )
        self.content1 = CourseContent.objects.create(
            name="Konten 1", description="Deskripsi", course_id=self.course
        )
        self.content2 = CourseContent.objects.create(
            name="Konten 2", description="Deskripsi", course_id=self.course
        )
        
        # Enroll student
        CourseMember.objects.create(
            course_id=self.course, user_id=self.student, roles='std'
        )
        
        # Token untuk student
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "siswa", "password": "password"}
        )
        self.student_token = token_res.json()["access"]
    
    def test_mark_content_complete_success(self):
        response = self.client.post(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ContentCompletion.objects.filter(user=self.student, content=self.content1).exists())
    
    def test_mark_content_complete_duplicate(self):
        # Tandai pertama kali
        self.client.post(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        # Tandai kedua kali
        response = self.client.post(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("sudah menyelesaikan", response.json()["detail"])
    
    def test_mark_content_complete_not_member(self):
        # Buat user lain yang bukan member
        other_user = User.objects.create_user(
            username='other', email='other@sekolah.id', password='password'
        )
        token_res = self.client.post(
            "/api/auth/token/pair",
            json={"username": "other", "password": "password"}
        )
        other_token = token_res.json()["access"]
        
        response = self.client.post(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {other_token}"}
        )
        self.assertEqual(response.status_code, 403)
    
    def test_get_content_completions(self):
        # Tandai dua konten sebagai selesai
        ContentCompletion.objects.create(user=self.student, content=self.content1)
        ContentCompletion.objects.create(user=self.student, content=self.content2)
        
        response = self.client.get(
            f"/courses/{self.course.id}/completions",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        content_ids = [c['id'] for c in data]
        self.assertIn(self.content1.id, content_ids)
        self.assertIn(self.content2.id, content_ids)
    
    def test_get_content_completions_pagination(self):
        # Buat 15 konten
        contents = []
        for i in range(15):
            content = CourseContent.objects.create(
                name=f"Konten {i}", 
                description="Deskripsi", 
                course_id=self.course
            )
            contents.append(content)
            ContentCompletion.objects.create(user=self.student, content=content)
        
        # Request halaman 1
        response = self.client.get(
            f"/courses/{self.course.id}/completions?page=1",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 10)  # Default page size
        
        # Request halaman 2
        response = self.client.get(
            f"/courses/{self.course.id}/completions?page=2",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 5)
    
    def test_unmark_content_complete_success(self):
        # Tandai dulu
        ContentCompletion.objects.create(user=self.student, content=self.content1)
        
        response = self.client.delete(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(ContentCompletion.objects.filter(user=self.student, content=self.content1).exists())
    
    def test_unmark_content_complete_not_found(self):
        # Tidak ada completion
        response = self.client.delete(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 404)
    
    def test_unmark_content_complete_other_user(self):
        # Buat user lain
        other_user = User.objects.create_user(
            username='other', email='other@sekolah.id', password='password'
        )
        # Buat completion oleh user lain
        ContentCompletion.objects.create(user=other_user, content=self.content1)
        
        # Student mencoba hapus completion milik orang lain
        response = self.client.delete(
            f"/contents/{self.content1.id}/complete",
            headers={"Authorization": f"Bearer {self.student_token}"}
        )
        self.assertEqual(response.status_code, 404)