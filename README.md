## amiimporter -- A tool to help import vagrant box files as AWS ec2 images.

With this tool you can convert your vagrant box file to an AWS AMI (i.e. an ec2 image).

Using an S3 bucket of your choice it will import the box and produce AMIs in three regions, `eu-central-1`, `us-west-2` and `us-east-1`. The target regions can be easily adjusted.


### Prerequisites and limitations

- A vagrant box using virtualbox.

- Your vagrant box has [cloud-init](https://cloudinit.readthedocs.org/en/latest/) installed and configured. `ec2-user` is a good candidate for the user where the boot key will be installed.

- The produced AMIs are suitable for [HVM virtualization](http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/virtualization_types.html). pv requires more steps such as installing a pv enabled kernel.

- Define an s3 bucket (in a region close to you, to speed up uploads.)
  This will be used to upload the images for conversion to AMI.

- Define roles and policies in AWS. In particular:
  - a `vmimport` service role and a policy attached to it, precisely as explained [in this AWS doc.](http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/VMImportPrerequisites.html).

  - if you are an IAM AWS user (as opposed to root user) you **also** need to attach the following inline policy. Replace `<youraccountid>` [with your own](http://docs.aws.amazon.com/general/latest/gr/acct-identifiers.html).

    ```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "380",
            "Effect": "Allow",
            "Action": [
                "iam:CreateRole",
                "iam:PutRolePolicy"
            ],
            "Resource": [
                "arn:aws:iam::<youraccountid>:role/vmimport"
            ]
        }
    ]
}
    ```
- Fast upstream bandwidth as you will be uploading the image to s3!

### The required parameters are:

- `--vboxfile`
  Path to the vagrant .box file you wish to convert to AMI. Currently only virtualbox providers are supported.

- `--s3bucket`
  `[--s3key]`

  The s3bucket and the (temporary) key used for uploading the VM.
  `s3key` is optional but if you omit it, `vboxfile` expect a certain naming convention like `osdistroVER-othermetadata.box`
  For example ./oel7.1-x86_64-virtualbox.box is a valid name.

- `--verbose`

  Displays progress statistics. Very useful if the script is not run from another program.

By default it will created copies of the temporary AMI that AWS import-image creates in three regions -- us-east-1, us-west-2, eu-central-1.
It easy to add or remove destination regions in [this list](https://github.com/dliappis/amiimporter/blob/master/amiimporter.py#L173)

#### Example
For an existing oracle linux vagrant box:

``` shell
$ ./amiimporter.py --s3bucket mybucket --vboxfile ./oel7.1-x86_64-virtualbox.box --verbose
INFO:root:Uploading ./tmpdir/packer-virtualbox-iso-1453910880-disk1.vmdk to s3
99%
INFO:root:Running: aws --region eu-west-1 ec2 import-image --cli-input-json {"Description": "temp-hvm-oel-7-20160129134521", "DiskContainers": [{"UserBucket": {"S3Bucket": "mybucket", "S3Key": "temp-hvm-oel-7-20160129134521"}, "Description": "temp-hvm-oel-7-20160129134521"}]}
INFO:root:AWS is now importing vdmk to AMI.
98%
INFO:root:Done, amiid is ami-TTTTTTTT
INFO:root:Successfully created temporary AMI ami-TTTTTTTT
Created ami-XXXXXXXX in region eu-central-1
Created ami-YYYYYYYY in region us-west-2
Created ami-ZZZZZZZZ in region us-east-1
INFO:root:Deregistering temporary AMI ami-TTTTTTTT
INFO:root:Deleting updateded s3 file s3://mybucket/temp-hvm-oel-7-20160129134521

```
