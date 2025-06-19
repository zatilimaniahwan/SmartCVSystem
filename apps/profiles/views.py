import os
import shutil
import boto3
import urllib.parse
import logging
from botocore.exceptions import ClientError
from django.shortcuts import render, redirect
from django.views import View
from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm
from django.utils import timezone
from django.conf import settings
from django.db.models import Q

from apps.authx.auth_utils import login_required_custom, SessionRequiredMixin
from apps.authx.models import User, UsersRole, UserProfile, Company, CompanyUser
from apps.jobposts.models import JobPost, UserJob
from apps.profiles.models import ResumeFile, ParsedData
from apps.profiles.forms import ResumeForm, ProfileForm
from apps.profiles.utils import extract_resume_info_from_s3

def delete_s3_file(file_url):
    try:
        s3 = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
        )
        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
        parsed_url = urllib.parse.urlparse(str(file_url))
        s3_key = parsed_url.path.lstrip('/')
        s3.head_object(Bucket=bucket_name, Key=s3_key)
        s3.delete_object(Bucket=bucket_name, Key=s3_key)
    except ClientError as e:
        if e.response['Error']['Code'] != "404":
            raise

def delete_local_file(file_path):
    if file_path and hasattr(file_path, 'path') and os.path.exists(file_path.path):
        os.remove(file_path.path)
        media_root = settings.MEDIA_ROOT
        if os.path.exists(media_root) and not os.listdir(media_root):
            shutil.rmtree(media_root)

def get_latest_parsed_data(resume):
    try:
        return resume.parsed_data.order_by('-ParsedAt').first().Data
    except Exception:
        return None

def get_recommended_jobs(parsed_data):
    if not parsed_data:
        return []
    skills_str = parsed_data.get('skills', '')
    skills = [s.strip().lower() for s in skills_str.split(',') if s.strip()]
    education_level = parsed_data.get('education_level', '')
    experience_level = parsed_data.get('experience_level', '')

    exp_map = {'junior': 'Entry', 'mid': 'Mid', 'senior': 'Senior'}
    edu_map = {
        "Bachelor's": 'BA',
        "Master's": 'MA',
        "PhD": 'PhD',
        "High School": 'HS',
        "Diploma": 'HS',
    }
    jobposts = JobPost.objects.filter(IsActive=True)
    if education_level in edu_map:
        jobposts = jobposts.filter(EducationLevel=edu_map[education_level])
    if experience_level.lower() in exp_map:
        jobposts = jobposts.filter(ExperienceLevel=exp_map[experience_level.lower()])
    if skills:
        skill_q = Q()
        for skill in skills:
            skill_q |= Q(RequiredSkills__icontains=skill)
        jobposts = jobposts.filter(skill_q)
    return jobposts.distinct()

class DetailView(View):
    def get(self, request):
        return render(request, "profile/detailView.html")

class ManageResumesView(SessionRequiredMixin, View):
    def get(self, request):
        user_id = request.session['user_id']
        resume_form = ResumeForm()
        resumes = ResumeFile.objects.filter(UserID=user_id).order_by('-UploadedAt')
        return render(request, 'profile/resume.html', {
            'resume_form': resume_form,
            'resumes': resumes,
        })

    def post(self, request):
        user_id = request.session['user_id']
        action = request.POST.get('action')

        if action == 'upload':
            form = ResumeForm(request.POST, request.FILES)
            if form.is_valid():
                rf = form.save(commit=False)
                rf.UserID_id = user_id
                rf.Status = 'Uploaded'
                rf.save()
                local_file_path = rf.FilePath.path
                if not settings.DEBUG:
                    with open(local_file_path, 'rb') as resume_file:
                        s3 = boto3.client(
                            's3',
                            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                            region_name=settings.AWS_S3_REGION_NAME
                        )
                        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
                        s3_path = f"resumes/{user_id}/{os.path.basename(local_file_path)}"
                        s3.upload_fileobj(resume_file, bucket_name, s3_path, ExtraArgs={'ContentType': 'application/pdf'})
                        s3_url = f"https://{bucket_name}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com/{s3_path}"
                        rf.FilePath = s3_url
                        rf.save()
                        delete_local_file(rf.FilePath)
                        messages.success(request, "Resume uploaded")
                else:
                    messages.success(request, "Resume uploaded.")
            else:
                return render(request, 'profile/resume.html', {'resume_form': form})

        elif action == 'select':
            sel_id = request.POST.get('selected_id')
            ResumeFile.objects.filter(UserID=user_id, IsSelected=True).update(IsSelected=False)
            ResumeFile.objects.filter(pk=sel_id, UserID=user_id).update(IsSelected=True)
            messages.success(request, "Selected resume updated.")

        elif action == 'delete':
            sel_id = request.POST.get('selected_id')
            if sel_id:
                try:
                    resume = ResumeFile.objects.get(pk=sel_id, UserID=user_id)
                    ParsedData.objects.filter(ResumeID=resume).delete()
                    if not settings.DEBUG and resume.FilePath and str(resume.FilePath).startswith("http"):
                        delete_s3_file(resume.FilePath)
                    else:
                        delete_local_file(resume.FilePath)
                    resume.delete()
                    messages.success(request, "Selected resume deleted.")
                except ResumeFile.DoesNotExist:
                    messages.error(request, "Resume not found.")
                except Exception as e:
                    messages.error(request, f"Error deleting resume: {e}")
            else:
                messages.error(request, "No resume selected for deletion.")

        elif action == 'extract':
            sel_id = request.POST.get('selected_id')
            if sel_id:
                try:
                    resume = ResumeFile.objects.get(pk=sel_id, UserID=user_id)
                    if resume.FilePath and str(resume.FilePath).startswith("http"):
                        parsed_url = urllib.parse.urlparse(str(resume.FilePath))
                        s3_key = parsed_url.path.lstrip('/')
                        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
                        aws_access_key = settings.AWS_ACCESS_KEY_ID
                        aws_secret_key = settings.AWS_SECRET_ACCESS_KEY
                        region = settings.AWS_S3_REGION_NAME
                        resume_info = extract_resume_info_from_s3(
                            bucket_name, s3_key, aws_access_key, aws_secret_key, region
                        )
                        # Only save if at least one value is not 'unknown'
                        values = list(resume_info.values())
                        if any(v and str(v).lower() != "unknown" for v in values):
                            if not ParsedData.objects.filter(ResumeID=resume).exists():
                                ParsedData.objects.create(
                                    ResumeID=resume,
                                    Data=resume_info
                                )
                                messages.success(request, "Resume extracted and data saved successfully.")
                            else:
                                messages.info(request, "Parsed data for this resume already exists.")
                        else:
                            messages.warning(request, "Extraction failed or resume data is unknown. Nothing was saved.")
                    else:
                        messages.error(request, "Resume file is not available in S3 for extraction.")
                except ResumeFile.DoesNotExist:
                    messages.error(request, "Resume not found.")
                except Exception as e:
                    messages.error(request, f"Error extracting resume: {e}")
            else:
                messages.error(request, "No resume selected for extraction.")

        return redirect('profiles:resume')

class DashboardView(SessionRequiredMixin, View):
    def get(self, request):
        user_id = request.session['user_id']

        user    = User.objects.get(users_id=user_id)

        # Fetch all Roles assigned to this user
        roles = [ur.role.role_name.lower() for ur in UsersRole.objects.filter(user_id=user_id).select_related('role')]

        context = {
            'user':  user,
            'roles': roles,
        }
        print(roles)
        if 'candidate' in roles:
            # Candidate metrics
            resumes_qs     = ResumeFile.objects.filter(UserID_id=user_id)
            context.update({
                'resume_count':  resumes_qs.count(),
                'has_active':    resumes_qs.filter(IsSelected=True).exists(),
                'applied_count': UserJob.objects.filter(User_id=user_id, IsApplied=True).count(),
                'saved_count':   UserJob.objects.filter(User_id=user_id, IsSaved=True).count(),
            })

        if 'company hr' in roles or 'recruiter' in roles:
            # Company HR / Recruiter metrics
            context.update({
                'total_jobs':   JobPost.objects.filter(Recruiter_id=user_id).count(),

                'expired_jobs': JobPost.objects.filter(
                    Recruiter_id=user_id,
                    ApplicationDeadline__lt=timezone.now()
                ).count(),


                    JobPost__Recruiter_id=user_id,
                    IsApplied=True
                ).count(),
            })

        return render(request, 'dashboard.html', context)

class ChangePasswordView(SessionRequiredMixin, View):
    def get(self, request):
        user = User.objects.get(users_id=request.session['user_id'])
        form = PasswordChangeForm(user=user)
        return render(request, 'profile/password_change_form.html', {'form': form})

    def post(self, request):
        user = User.objects.get(users_id=request.session['user_id'])
        form = PasswordChangeForm(user=user, data=request.POST)
        if not form.is_valid():
            return render(request, 'profile/password_change_form.html', {'form': form})
        form.save()
        messages.success(request, "Your password has been updated.")
        return redirect('profiles:dashboard')

class ProfileEditView(SessionRequiredMixin, View):
    def get(self, request):
        user_id = request.session['user_id']
        profile = UserProfile.objects.get(user_id=user_id)
        role_names = UsersRole.objects.filter(user_id=user_id).values_list('role__role_name', flat=True)
        has_company_role = any(r.lower() in ('company hr', 'recruiter') for r in role_names)
        company = None
        if has_company_role:
            cu = CompanyUser.objects.filter(user_id=user_id).first()
            company = cu.company if cu else None
        initial = {
            'phone': profile.phone,
            'location': profile.location,
        }
        if company:
            initial.update({
                'company_name': company.name,
                'company_address': company.address,
                'company_website': company.website,
            })
        form = ProfileForm(initial=initial)
        return render(request, 'profile/profile_edit.html', {
            'form': form,
            'profile': profile,
            'has_company_role': has_company_role,
        })

    def post(self, request):
        user_id = request.session['user_id']
        profile = UserProfile.objects.get(user_id=user_id)
        role_names = UsersRole.objects.filter(user_id=user_id).values_list('role__role_name', flat=True)
        has_company_role = any(r.lower() in ('company hr', 'recruiter') for r in role_names)
        form = ProfileForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, 'profile/profile_edit.html', {
                'form': form,
                'profile': profile,
                'has_company_role': has_company_role,
            })
        profile.phone = form.cleaned_data['phone']
        profile.location = form.cleaned_data['location']
        avatar = form.cleaned_data.get('avatar')
        if avatar:
            profile.avatar = avatar
        profile.save()
        if has_company_role:
            cu = CompanyUser.objects.filter(user_id=user_id).first()
            if cu:
                company = cu.company
            else:
                company = Company.objects.create(
                    name=form.cleaned_data['company_name'],
                    address=form.cleaned_data['company_address'],
                    website=form.cleaned_data['company_website'],
                )
                CompanyUser.objects.create(
                    user_id=user_id,
                    company_id=company.id,
                )
            company.name = form.cleaned_data['company_name']
            company.address = form.cleaned_data['company_address']
            company.website = form.cleaned_data['company_website']
            company.save()
        messages.success(request, "Your profile has been updated.")
        return redirect('profiles:profile_edit')