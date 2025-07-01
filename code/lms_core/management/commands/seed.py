from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from lms_core.models import Course

class Command(BaseCommand):
    help = 'Seed initial teacher and course data'

    def handle(self, *args, **options):
        # Create or get teacher
        teacher, created = User.objects.get_or_create(
            username='teacher1',
            defaults={
                'email': 'teacher1@example.com',
                'first_name': 'Teacher',
                'last_name': 'One',
                'is_staff': True,
                'is_active': True
            }
        )
        
        if created:
            teacher.set_password('password123')
            teacher.save()
            self.stdout.write(self.style.SUCCESS('Created teacher account'))
        else:
            self.stdout.write('Teacher account already exists')

        # Create or get course
        course, created = Course.objects.get_or_create(
            name='Matematika Dasar',
            defaults={
                'description': 'Kursus matematika dasar untuk pemula',
                'price': 500000,
                'teacher': teacher
            }
        )

        if created:
            self.stdout.write(self.style.SUCCESS('Created course'))
        else:
            self.stdout.write('Course already exists')

        self.stdout.write(self.style.SUCCESS('Successfully seeded data'))